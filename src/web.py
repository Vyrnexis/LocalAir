"""LocalAIR web service and dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import os
from pathlib import Path
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import AppConfig, database_path, load_config, save_config
from .device import (
    ClimateDecision,
    CommandResult,
    ConnectionStatus,
    DeviceMonitor,
    DeviceReading,
    IR_COMMANDS,
    TuyaDevice,
    PIU14_CODES_PATH,
    decide_climate,
    local_ir_instruction,
    update_ac_state,
)
from .storage import RANGE_HOURS, LocalAirStore


STATIC_DIR = Path(__file__).resolve().parent / "static"
ICON_PATH = STATIC_DIR / "localair.png"

# Map validation messages to the Settings field they belong to.
VALIDATION_FIELDS = {
    "IP address is required.": "ip_address",
    "Device ID is required.": "device_id",
    "Protocol version must be between 3.1 and 3.5.": "protocol_version",
    "Poll interval must be between 5 and 3600 seconds.": (
        "poll_interval_seconds"
    ),
    "Schedule times must use HH:MM format.": "climate_start",
    "Schedule start must be before schedule end.": "climate_end",
    "The turn-off temperature must be lower than the turn-on temperature.": (
        "turn_off_at"
    ),
    "Air-conditioner temperature must be 16–30°C.": "aircon_temperature",
    "Sensor timeout must be at least the polling interval.": (
        "sensor_timeout_seconds"
    ),
    "Server port must be between 1 and 65535.": "server_port",
    "History retention must be between 1 and 365 days.": (
        "history_retention_days"
    ),
}


class SettingsError(ValueError):
    """A settings validation error tied to one form field."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def _number(data: dict[str, Any], key: str, current, kind, label: str):
    value = data.get(key, current)
    try:
        return kind(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{label} must be a number.", key) from None


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _validated(config: AppConfig) -> AppConfig:
    try:
        return config.validated()
    except SettingsError:
        raise
    except ValueError as exc:
        raise SettingsError(str(exc), VALIDATION_FIELDS.get(str(exc))) from None


class LocalAirService:
    """Runtime state shared by the device monitor and web requests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config = load_config()
        self.store = LocalAirStore(
            database_path(), self.config.history_retention_days
        )
        self.latest_reading = DeviceReading(
            temperature_c=None,
            humidity_percent=None,
            timestamp=datetime.now().astimezone(),
            connection_status=ConnectionStatus.DISCONNECTED,
            error_message="Starting LocalAIR.",
        )
        self.last_result: CommandResult | None = None
        self.last_ir: tuple[str, str] | None = None
        self.last_valid_reading: DeviceReading | None = None
        self.climate = ClimateDecision(
            "Disabled", "Automation is off.", None
        )
        self.last_climate_attempt: tuple[str, float] | None = None
        self.climate_active = False
        self.climate_shutdown_pending = False
        self.climate_event = threading.Event()
        self.stopping = False
        self.climate_thread = threading.Thread(
            target=self._climate_loop,
            name="localair-climate",
            daemon=True,
        )
        self.monitor = DeviceMonitor(
            self.config,
            self._on_reading,
            self._on_ir,
        )

    def start(self) -> None:
        self.monitor.start()
        self.climate_thread.start()
        self.monitor.refresh()

    def stop(self) -> None:
        self.stopping = True
        self.climate_event.set()
        self.climate_thread.join(timeout=3)
        self.monitor.stop()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            reading = self.latest_reading
            result = self.last_result
            config = self.config
            last_ir = self.last_ir
            climate = self.climate
            last_valid = self.last_valid_reading
        local_ir_ready = bool(config.local_key and PIU14_CODES_PATH.is_file())
        return {
            "brand": "LocalAIR",
            "device": {
                "ip_address": config.ip_address,
                "poll_interval_seconds": config.poll_interval_seconds,
                "timezone": datetime.now().astimezone().tzname()
                or "Local time",
            },
            "reading": {
                "temperature_c": reading.temperature_c,
                "humidity_percent": reading.humidity_percent,
                "timestamp": reading.timestamp.isoformat(timespec="seconds"),
                "status": reading.connection_status.value,
                "ok": reading.ok,
                "error_message": reading.error_message,
            },
            "last_valid": (
                {
                    "temperature_c": last_valid.temperature_c,
                    "humidity_percent": last_valid.humidity_percent,
                    "timestamp": last_valid.timestamp.isoformat(
                        timespec="seconds"
                    ),
                }
                if last_valid
                else None
            ),
            "ac_state": dict(config.ac_state),
            "climate": {
                "enabled": config.climate_enabled,
                "status": climate.status,
                "detail": climate.detail,
                "start_time": config.climate_start,
                "end_time": config.climate_end,
                "turn_on_at": config.turn_on_at,
                "turn_off_at": config.turn_off_at,
                "aircon_temperature": config.aircon_temperature,
            },
            "ir_commands": {
                name: local_ir_ready
                for name in sorted(IR_COMMANDS)
            },
            "last_result": asdict(result) if result else None,
            "last_ir": (
                {"command": last_ir[0], "timestamp": last_ir[1]}
                if last_ir
                else None
            ),
            "events": self.store.recent_activity_events(30),
        }

    def send_ir(self, command: str) -> CommandResult:
        if command not in IR_COMMANDS:
            raise ValueError("Unknown IR command.")
        with self.lock:
            config = self.config
        key_name, message = local_ir_instruction(command, config.ac_state)
        result = self.monitor.send_ir(key_name)
        if result.ok:
            result = CommandResult(True, message)
        self._on_result(result)
        if result.ok:
            # A manual command after the window ends takes over from the
            # pending end-of-schedule Off.
            with self.lock:
                self.climate_shutdown_pending = False
            self._on_ir(command, "localair")
        return result

    def sync_power(self, power: str | None = None) -> dict[str, str]:
        with self.lock:
            current = self.config.ac_state.get("power")
            next_power = power or ("off" if current == "on" else "on")
            if next_power not in {"on", "off"}:
                raise ValueError("Power state must be on or off.")
            new_state = dict(self.config.ac_state)
            new_state["power"] = next_power
        self._save_state(new_state, "sync")
        return {"power": next_power}

    # Persist climate automation state and evaluate it immediately.
    def set_climate_enabled(self, enabled: bool) -> dict[str, bool]:
        with self.lock:
            config = replace(self.config, climate_enabled=enabled).validated()
        save_config(config)
        with self.lock:
            self.config = config
            self.last_climate_attempt = None
        self.monitor.update_config(config)
        self.climate_event.set()
        return {"enabled": enabled}

    # Settings as shown on the Settings page; the local key is never returned.
    def settings(self) -> dict[str, Any]:
        with self.lock:
            config = self.config
        return {
            "ip_address": config.ip_address,
            "device_id": config.device_id,
            "local_key_saved": bool(config.local_key),
            "protocol_version": config.protocol_version,
            "poll_interval_seconds": config.poll_interval_seconds,
            "climate_enabled": config.climate_enabled,
            "climate_start": config.climate_start,
            "climate_end": config.climate_end,
            "turn_on_at": config.turn_on_at,
            "turn_off_at": config.turn_off_at,
            "aircon_temperature": config.aircon_temperature,
            "sensor_timeout_seconds": config.sensor_timeout_seconds,
            "server_host": config.server_host,
            "server_port": config.server_port,
            "history_retention_days": config.history_retention_days,
            "env_overrides": {
                "server_host": "LOCALAIR_HOST" in os.environ,
                "server_port": "LOCALAIR_PORT" in os.environ,
            },
        }

    # Build a config from submitted connection fields, keeping the saved key
    # when the key field is blank.
    def _sensor_config(self, data: dict[str, Any]) -> AppConfig:
        with self.lock:
            current = self.config
        return replace(
            current,
            ip_address=str(data.get("ip_address", current.ip_address)),
            device_id=str(data.get("device_id", current.device_id)),
            local_key=str(data.get("local_key") or "").strip()
            or current.local_key,
            protocol_version=_number(
                data,
                "protocol_version",
                current.protocol_version,
                float,
                "Protocol version",
            ),
        )

    # Read the sensor with submitted connection details without saving them.
    def test_connection(self, data: dict[str, Any]) -> dict[str, Any]:
        config = _validated(self._sensor_config(data))
        with self.lock:
            current = self.config
            latest = self.latest_reading
        unchanged = (
            config.ip_address,
            config.device_id,
            config.local_key,
            config.protocol_version,
        ) == (
            current.ip_address,
            current.device_id,
            current.local_key,
            current.protocol_version,
        )
        # The live connection already reads these details, and many Tuya
        # devices accept only one local connection at a time.
        reading = latest if unchanged else TuyaDevice().get_status(config)
        return {
            "ok": reading.ok,
            "message": (
                "Connected."
                if reading.ok
                else reading.error_message or "Could not read the sensor."
            ),
            "temperature_c": reading.temperature_c,
            "humidity_percent": reading.humidity_percent,
        }

    def update_settings(self, data: dict[str, Any]) -> None:
        current = self._sensor_config(data)
        config = _validated(
            replace(
                current,
                poll_interval_seconds=_number(
                    data,
                    "poll_interval_seconds",
                    current.poll_interval_seconds,
                    int,
                    "Read interval",
                ),
                climate_enabled=_flag(
                    data.get("climate_enabled", current.climate_enabled)
                ),
                climate_start=str(
                    data.get("climate_start", current.climate_start)
                ),
                climate_end=str(data.get("climate_end", current.climate_end)),
                turn_on_at=_number(
                    data, "turn_on_at", current.turn_on_at, float,
                    "Turn-on temperature",
                ),
                turn_off_at=_number(
                    data, "turn_off_at", current.turn_off_at, float,
                    "Turn-off temperature",
                ),
                aircon_temperature=_number(
                    data,
                    "aircon_temperature",
                    current.aircon_temperature,
                    int,
                    "AC temperature",
                ),
                sensor_timeout_seconds=_number(
                    data,
                    "sensor_timeout_seconds",
                    current.sensor_timeout_seconds,
                    int,
                    "Sensor timeout",
                ),
                server_host=str(data.get("server_host", current.server_host)),
                server_port=_number(
                    data, "server_port", current.server_port, int, "Web port"
                ),
                history_retention_days=_number(
                    data,
                    "history_retention_days",
                    current.history_retention_days,
                    int,
                    "History",
                ),
            )
        )
        save_config(config)
        with self.lock:
            self.config = config
            self.store.retention_days = config.history_retention_days
            self.last_climate_attempt = None
        self.monitor.update_config(config)
        self.monitor.refresh()
        self.climate_event.set()

    def _on_reading(self, reading: DeviceReading) -> None:
        with self.lock:
            self.latest_reading = reading
            if reading.ok:
                self.last_valid_reading = reading
        self.store.add_reading(reading)

    def _on_ir(
        self, command: str, source: str, event: str | None = None
    ) -> None:
        with self.lock:
            config = self.config
            state = update_ac_state(config.ac_state, command)
            if command == "climate_on":
                state["temperature"] = str(config.aircon_temperature)
            event_command = command
            if command == "power":
                event_command = f"power_{state.get('power', 'unknown')}"
            elif command in {"temp_up", "temp_down"}:
                temperature = state.get("temperature", "unknown")
                event_command = f"temperature_{temperature}"
            event_command = event or event_command
        self.store.add_ir_event(source, event_command)
        self._save_state(state, f"ir:{command}")
        with self.lock:
            self.last_ir = (
                event_command,
                datetime.now().astimezone().isoformat(timespec="seconds"),
            )

    def _on_result(self, result: CommandResult) -> None:
        with self.lock:
            self.last_result = result

    # Evaluate schedule and sensor state every ten seconds or after settings change.
    def _climate_loop(self) -> None:
        while not self.stopping:
            try:
                self._evaluate_climate()
            except Exception as exc:
                self._on_result(
                    CommandResult(
                        False,
                        f"Climate control failed ({type(exc).__name__}).",
                    )
                )
            self.climate_event.wait(10)
            self.climate_event.clear()

    # Apply only state transitions and retry failed local commands after one minute.
    # When the schedule ends, send one Off and then leave the AC to manual control.
    def _evaluate_climate(self) -> None:
        now = datetime.now().astimezone()
        with self.lock:
            config = self.config
            reading = self.last_valid_reading
            power = config.ac_state.get("power", "off")
        decision = decide_climate(config, reading, power, now)
        active = decision.desired_power is not None
        with self.lock:
            self.climate = decision
            if self.climate_active and not active and config.climate_enabled:
                self.climate_shutdown_pending = True
                self.last_climate_attempt = None
            elif active or not config.climate_enabled:
                self.climate_shutdown_pending = False
            self.climate_active = active
            shutdown = self.climate_shutdown_pending
            last_attempt = self.last_climate_attempt
        desired_power = "off" if shutdown else decision.desired_power
        if desired_power is None:
            return

        command = f"climate_{desired_power}"
        first_confirmation = last_attempt is None
        if power == desired_power and not first_confirmation and not shutdown:
            return
        if last_attempt and last_attempt[0] == command:
            if time.monotonic() - last_attempt[1] < 60:
                return
        with self.lock:
            self.last_climate_attempt = (command, time.monotonic())
        key_name, message = local_ir_instruction(
            command,
            config.ac_state,
            config.aircon_temperature,
        )
        result = self.monitor.send_ir(key_name)
        if result.ok:
            result = CommandResult(True, message)
        self._on_result(result)
        if result.ok:
            if shutdown:
                with self.lock:
                    self.climate_shutdown_pending = False
            self._on_ir(
                command, "climate", "schedule_end" if shutdown else None
            )

    def _save_state(self, state: dict[str, str], source: str) -> None:
        with self.lock:
            old_state = dict(self.config.ac_state)
            self.config = replace(self.config, ac_state=state)
            config = self.config
        save_config(config)
        self.store.add_state_events(source, old_state, state)


service: LocalAirService | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global service
    service = LocalAirService()
    service.start()
    try:
        yield
    finally:
        service.stop()


app = FastAPI(title="LocalAIR", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Revalidate pages and scripts so an updated image is picked up immediately.
@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in {"/", "/settings"} or (
        path.startswith("/static/") and not path.startswith("/static/fonts/")
    ):
        response.headers["Cache-Control"] = "no-cache"
    return response


def get_service() -> LocalAirService:
    if service is None:
        raise HTTPException(status_code=503, detail="LocalAIR is starting.")
    return service


def _settings_error(exc: SettingsError) -> JSONResponse:
    return JSONResponse(
        {"detail": str(exc), "field": exc.field}, status_code=400
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/settings")
def settings() -> FileResponse:
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(ICON_PATH, media_type="image/png")


@app.get("/localair.png")
def icon() -> FileResponse:
    return FileResponse(ICON_PATH, media_type="image/png")


@app.get("/api/settings")
def api_settings() -> JSONResponse:
    return JSONResponse(get_service().settings())


@app.post("/api/settings")
async def api_save_settings(request: Request) -> JSONResponse:
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    try:
        get_service().update_settings(data)
    except SettingsError as exc:
        return _settings_error(exc)
    return JSONResponse({"ok": True, "settings": get_service().settings()})


@app.post("/api/settings/test")
def api_test_settings(data: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(get_service().test_connection(data))
    except SettingsError as exc:
        return _settings_error(exc)


@app.get("/api/status")
def api_status() -> JSONResponse:
    return JSONResponse(get_service().snapshot())


@app.get("/api/history")
def api_history(range: str = "24h") -> JSONResponse:
    current = get_service()
    store = current.store
    now = datetime.now().astimezone()
    start = now - timedelta(hours=RANGE_HOURS.get(range, 24))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    power = current.config.ac_state.get("power", "off")
    return JSONResponse(
        {
            "start": start.isoformat(timespec="seconds"),
            "end": now.isoformat(timespec="seconds"),
            "points": store.history(range),
            "events": store.history_events(range),
            "ac_runs": store.power_runs(start, now, power),
            "today": {
                "start": midnight.isoformat(timespec="seconds"),
                "runs": store.power_runs(midnight, now, power),
            },
        }
    )


@app.post("/api/ir/{command}")
def api_ir(command: str) -> JSONResponse:
    try:
        result = get_service().send_ir(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse(asdict(result), status_code=200 if result.ok else 502)


@app.post("/api/sync/power")
def api_sync_power() -> JSONResponse:
    return JSONResponse({"ok": True, "state": get_service().sync_power()})


@app.post("/api/sync/power/{power}")
# Set the assumed AC power state without sending a device command.
def api_set_power(power: str) -> JSONResponse:
    try:
        state = get_service().sync_power(power)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"ok": True, "state": state})


@app.post("/api/climate/{state}")
# Enable or disable automatic climate control from the dashboard.
def api_set_climate(state: str) -> JSONResponse:
    if state not in {"on", "off"}:
        raise HTTPException(status_code=400, detail="State must be on or off.")
    return JSONResponse(
        {"ok": True, **get_service().set_climate_enabled(state == "on")}
    )
