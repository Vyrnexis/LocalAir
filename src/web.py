"""LocalAIR web service and dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from .config import AppConfig, database_path, load_config, save_config
from .device import (
    ClimateDecision,
    CommandResult,
    ConnectionStatus,
    DeviceMonitor,
    DeviceReading,
    IR_COMMANDS,
    TuyaDevice,
    decide_climate,
    update_ac_state,
)
from .storage import LocalAirStore


STATIC_DIR = Path(__file__).resolve().parent / "static"
ICON_PATH = STATIC_DIR / "localair.png"


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
        self.cloud_lock = threading.Lock()
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
        cloud_ready = all(
            (
                config.cloud_api_key,
                config.cloud_api_secret,
                config.aircon_device_id,
            )
        )
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
                name: cloud_ready
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
        with self.cloud_lock:
            result = TuyaDevice().send_ir(config, command)
        self._on_result(result)
        if result.ok:
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

    def update_settings(self, data: dict[str, str]) -> None:
        with self.lock:
            current = self.config
        config = replace(
            current,
            ip_address=data.get("ip_address", current.ip_address),
            device_id=data.get("device_id", current.device_id),
            local_key=data.get("local_key", "").strip() or current.local_key,
            protocol_version=float(
                data.get("protocol_version", current.protocol_version)
            ),
            poll_interval_seconds=int(
                data.get(
                    "poll_interval_seconds",
                    current.poll_interval_seconds,
                )
            ),
            cloud_region=data.get("cloud_region", current.cloud_region),
            cloud_api_key=(
                data.get("cloud_api_key", "").strip()
                or current.cloud_api_key
            ),
            cloud_api_secret=(
                data.get("cloud_api_secret", "").strip()
                or current.cloud_api_secret
            ),
            aircon_device_id=data.get(
                "aircon_device_id", current.aircon_device_id
            ),
            climate_enabled=data.get("climate_enabled") == "on",
            climate_start=data.get("climate_start", current.climate_start),
            climate_end=data.get("climate_end", current.climate_end),
            turn_on_at=float(data.get("turn_on_at", current.turn_on_at)),
            turn_off_at=float(data.get("turn_off_at", current.turn_off_at)),
            aircon_temperature=int(
                data.get(
                    "aircon_temperature", current.aircon_temperature
                )
            ),
            sensor_timeout_seconds=int(
                data.get(
                    "sensor_timeout_seconds",
                    current.sensor_timeout_seconds,
                )
            ),
            server_host=data.get("server_host", current.server_host),
            server_port=int(data.get("server_port", current.server_port)),
            history_retention_days=int(
                data.get(
                    "history_retention_days",
                    current.history_retention_days,
                )
            ),
        ).validated()
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

    def _on_ir(self, command: str, source: str) -> None:
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

    # Apply only state transitions and retry failed cloud commands after one minute.
    def _evaluate_climate(self) -> None:
        now = datetime.now().astimezone()
        with self.lock:
            config = self.config
            reading = self.last_valid_reading
            power = config.ac_state.get("power", "off")
            last_attempt = self.last_climate_attempt
        decision = decide_climate(config, reading, power, now)
        with self.lock:
            self.climate = decision
        if decision.desired_power is None:
            return

        command = f"climate_{decision.desired_power}"
        first_confirmation = last_attempt is None
        if power == decision.desired_power and not first_confirmation:
            return
        if last_attempt and last_attempt[0] == command:
            if time.monotonic() - last_attempt[1] < 60:
                return
        with self.lock:
            self.last_climate_attempt = (command, time.monotonic())
        with self.cloud_lock:
            result = TuyaDevice().set_climate(
                config, decision.desired_power == "on"
            )
        self._on_result(result)
        if result.ok:
            self._on_ir(command, "climate")

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


def get_service() -> LocalAirService:
    if service is None:
        raise HTTPException(status_code=503, detail="LocalAIR is starting.")
    return service


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return DASHBOARD_HTML


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(ICON_PATH, media_type="image/png")


@app.get("/localair.png")
def icon() -> FileResponse:
    return FileResponse(ICON_PATH, media_type="image/png")


@app.get("/settings", response_class=HTMLResponse)
def settings() -> str:
    config = get_service().config
    return SETTINGS_HTML.format(
        ip_address=html_escape(config.ip_address),
        device_id=html_escape(config.device_id),
        protocol_version=config.protocol_version,
        poll_interval_seconds=config.poll_interval_seconds,
        server_host=html_escape(config.server_host),
        server_port=config.server_port,
        history_retention_days=config.history_retention_days,
        cloud_region=html_escape(config.cloud_region),
        aircon_device_id=html_escape(config.aircon_device_id),
        climate_enabled="checked" if config.climate_enabled else "",
        climate_start=config.climate_start,
        climate_end=config.climate_end,
        turn_on_at=config.turn_on_at,
        turn_off_at=config.turn_off_at,
        aircon_temperature=config.aircon_temperature,
        sensor_timeout_seconds=config.sensor_timeout_seconds,
    )


@app.post("/settings")
async def save_settings(request: Request) -> RedirectResponse:
    body = (await request.body()).decode()
    data = {key: values[-1] for key, values in parse_qs(body).items()}
    try:
        get_service().update_settings(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse("/", status_code=303)


@app.get("/api/status")
def api_status() -> JSONResponse:
    return JSONResponse(get_service().snapshot())


@app.get("/api/history")
def api_history(range: str = "24h") -> JSONResponse:
    store = get_service().store
    return JSONResponse(
        {
            "points": store.history(range),
            "events": store.history_events(range),
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


def html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LocalAIR</title>
  <link rel="icon" type="image/png" href="/localair.png">
  <link rel="apple-touch-icon" href="/localair.png">
  <script>
    document.documentElement.dataset.theme = localStorage.getItem("theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  </script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7fb;
      --card: rgba(255, 255, 255, .82);
      --surface: #edf2f8;
      --text: #111827;
      --muted: #64748b;
      --line: rgba(148, 163, 184, .24);
      --grid-line: #e4eaf1;
      --blue: #3578f6;
      --orange: #f36f45;
      --green: #16a678;
      --red: #dc4c4c;
      --shadow: 0 20px 55px rgba(43, 58, 85, .10);
    }
    :root[data-theme="dark"] {
      color-scheme: dark;
      --bg: #090d14;
      --card: rgba(20, 27, 39, .84);
      --surface: #1b2432;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --line: rgba(148, 163, 184, .16);
      --grid-line: #273244;
      --blue: #66a0ff;
      --orange: #ff8561;
      --green: #34d399;
      --red: #fb7185;
      --shadow: 0 24px 70px rgba(0, 0, 0, .34);
    }
    * { box-sizing: border-box; }
    body {
      min-height: 100vh; margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 8% 0%, rgba(53, 120, 246, .16), transparent 32rem),
        radial-gradient(circle at 92% 12%, rgba(52, 211, 153, .09), transparent 28rem),
        var(--bg);
      color: var(--text);
      transition: background-color .25s ease, color .25s ease;
    }
    main { width: min(1420px, calc(100% - 32px)); margin: 0 auto; padding: 22px 0 42px; }
    header {
      display: flex; align-items: center; justify-content: space-between;
      gap: 16px; margin-bottom: 22px;
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .mark {
      width: 46px; height: 46px; border-radius: 14px;
      display: grid; place-items: center;
      background: linear-gradient(145deg, #2469ed, #68b6ff);
      color: white; box-shadow: 0 12px 28px rgba(53, 120, 246, .26);
      font-size: 13px; font-weight: 900; letter-spacing: .06em;
    }
    h1 { margin: 0; font-size: 27px; letter-spacing: -.045em; }
    .tagline { margin-top: 1px; color: var(--muted); font-size: 14px; }
    .header-actions { display: flex; align-items: center; gap: 8px; }
    a { color: inherit; text-decoration: none; font-weight: 700; }
    .quiet-button, .settings-link {
      min-height: 40px; padding: 9px 13px; border: 1px solid var(--line);
      border-radius: 12px; color: var(--text); background: var(--card);
      backdrop-filter: blur(18px);
    }
    .status-bar { display: flex; align-items: center; gap: 12px; padding: 10px 14px; border: 1px solid var(--line); border-radius: 14px; background: var(--card); color: var(--muted); font-size: 13px; }
    .status-spacer { flex: 1; }
    .dot { width: 9px; height: 9px; border-radius: 99px; background: #94a3b8; display: inline-block; }
    .dot.ok { background: var(--green); }
    .dot.error { background: var(--red); }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; margin-top: 14px; }
    .card {
      background: var(--card); border: 1px solid var(--line);
      border-radius: 24px; padding: 22px; box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }
    .card h2 { margin: 0; font-size: 16px; letter-spacing: -.02em; }
    .eyebrow { color: var(--muted); font-size: 12px; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; }
    .hero-card { grid-column: span 6; min-height: 164px; position: relative; overflow: hidden; display: flex; flex-direction: column; justify-content: center; }
    .hero-card::after { content: ""; position: absolute; inset: auto -10% -55% 5%; height: 150px; border-radius: 50%; background: color-mix(in srgb, var(--blue) 9%, transparent); transform: rotate(-4deg); }
    .hero-title { margin: 7px 0 5px; color: var(--blue); font-size: clamp(30px, 4vw, 46px); line-height: 1; letter-spacing: -.055em; }
    .hero-summary { margin: 0; font-size: 17px; font-weight: 800; }
    .hero-detail { margin: 7px 0 0; color: var(--muted); }
    .metric-card { grid-column: span 3; min-height: 164px; display: flex; flex-direction: column; justify-content: space-between; }
    .metric { font-size: clamp(48px, 7vw, 76px); line-height: .9; font-weight: 820; letter-spacing: -.07em; }
    .temp { color: var(--orange); }
    .humidity { color: var(--blue); }
    .muted { color: var(--muted); }
    .ac-card { grid-column: span 6; display: grid; gap: 18px; }
    .climate-card { grid-column: span 6; display: grid; gap: 18px; }
    .card-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .state-pill { padding: 6px 10px; border-radius: 999px; background: var(--surface); color: var(--muted); font-size: 13px; font-weight: 800; }
    .state-pill.on { color: var(--green); background: color-mix(in srgb, var(--green) 13%, transparent); }
    .controls { display: grid; grid-template-columns: .8fr 1.2fr; gap: 12px; }
    .power-control, .temperature-control { min-height: 154px; border-radius: 18px; background: var(--surface); padding: 16px; }
    .power-control { display: grid; gap: 12px; }
    .temperature-control { display: grid; grid-template-columns: 56px 1fr 56px; align-items: center; gap: 12px; }
    .setpoint { text-align: center; }
    .setpoint strong { display: block; font-size: clamp(48px, 7vw, 72px); line-height: .95; letter-spacing: -.07em; }
    .setpoint span { display: block; margin-top: 8px; color: var(--muted); font-size: 13px; }
    .automation-panel { display: grid; gap: 16px; }
    .automation-state { display: flex; align-items: flex-start; gap: 12px; }
    .automation-dot { width: 11px; height: 11px; margin-top: 5px; border-radius: 50%; background: var(--muted); box-shadow: 0 0 0 5px color-mix(in srgb, var(--muted) 12%, transparent); }
    .automation-state.cooling .automation-dot { background: var(--green); box-shadow: 0 0 0 5px color-mix(in srgb, var(--green) 14%, transparent); }
    .automation-state.sensor-unavailable .automation-dot { background: var(--red); box-shadow: 0 0 0 5px color-mix(in srgb, var(--red) 14%, transparent); }
    .automation-title { font-size: 20px; font-weight: 850; letter-spacing: -.03em; }
    .automation-detail { margin-top: 4px; color: var(--muted); font-size: 14px; }
    .automation-values { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
    .automation-value { padding: 10px; border-radius: 14px; background: var(--card); }
    .automation-value span { display: block; color: var(--muted); font-size: 11px; font-weight: 800; letter-spacing: .05em; text-transform: uppercase; }
    .automation-value strong { display: block; margin-top: 4px; font-size: 17px; }
    .automation-value.next { grid-column: 1 / -1; }
    .manual-hint { margin: 0; font-size: 13px; }
    .toggle { width: 48px; height: 28px; padding: 3px; border-radius: 999px; background: var(--surface); border: 1px solid var(--line); }
    .toggle::after { content: ""; display: block; width: 20px; height: 20px; border-radius: 50%; background: var(--muted); transition: transform .2s ease, background .2s ease; }
    .toggle.on { background: var(--blue); }
    .toggle.on::after { background: white; transform: translateX(19px); }
    .sync-local { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 12px; border-top: 1px solid var(--line); }
    .sync-actions { display: flex; gap: 7px; }
    .sync-actions button { padding: 7px 11px; border: 1px solid var(--line); background: transparent; font-size: 12px; }
    .feedback { min-height: 20px; margin: 0; font-size: 13px; }
    .feedback.error { color: var(--red); }
    .feedback.success { color: var(--green); }
    button {
      border: 0; border-radius: 14px; padding: 11px 12px; font: inherit;
      font-weight: 800; color: var(--text); background: var(--surface); cursor: pointer;
      transition: transform .15s ease, opacity .15s ease, background .2s ease;
    }
    button:hover:not(:disabled) { transform: translateY(-1px); }
    button:active:not(:disabled) { transform: translateY(0); }
    .power-button { color: white; background: linear-gradient(145deg, #2469ed, #438cff); }
    .power-button.on { background: linear-gradient(145deg, #dc4c4c, #f16b6b); }
    .step-button { width: 56px; height: 56px; padding: 0; border: 1px solid var(--line); font-size: 28px; }
    button.sync { justify-self: start; padding: 0; color: var(--muted); background: transparent; font-size: 13px; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .chart-card { grid-column: 1 / -1; }
    .chart-tools { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .chart-actions { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
    .legend { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 13px; font-weight: 700; }
    .legend span { display: inline-flex; align-items: center; gap: 5px; }
    .legend i { width: 18px; height: 4px; border-radius: 999px; display: inline-block; }
    .legend .temp-line { background: var(--orange); }
    .legend .humidity-line { background: var(--blue); }
    select { border: 1px solid var(--line); border-radius: 12px; padding: 8px 10px; color: var(--text); background: var(--surface); }
    .chart-wrap { position: relative; margin-top: 10px; }
    canvas { width: 100%; height: 320px; display: block; touch-action: pan-y; }
    .chart-tooltip { position: absolute; z-index: 3; min-width: 180px; padding: 11px 13px; border-radius: 12px; color: #f8fafc; background: #172033; box-shadow: 0 14px 35px rgba(0,0,0,.28); pointer-events: none; transform: translate(-50%, calc(-100% - 14px)); font-size: 12px; line-height: 1.45; }
    .chart-tooltip strong { display: block; font-size: 13px; }
    .chart-tooltip[hidden] { display: none; }
    .events { grid-column: 1 / -1; }
    .events-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
    .event-filters { display: flex; gap: 6px; flex-wrap: wrap; }
    .filter { padding: 6px 10px; border: 1px solid var(--line); background: transparent; font-size: 12px; }
    .filter.active { color: var(--blue); border-color: var(--blue); background: color-mix(in srgb, var(--blue) 9%, transparent); }
    .event { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--line); }
    .event-main { display: flex; align-items: center; gap: 10px; }
    .event-marker { width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; background: var(--muted); }
    .event-marker.automatic { background: var(--blue); }
    .event-marker.manual { background: var(--orange); }
    .event-marker.errors { background: var(--red); }
    .event span:last-child { color: var(--muted); white-space: nowrap; }
    @media (max-width: 820px) {
      .hero-card { grid-column: span 12; }
      .metric-card { grid-column: span 6; }
      .ac-card, .climate-card { grid-column: span 12; }
    }
    @media (max-width: 620px) {
      main { width: min(100% - 20px, 1160px); padding-top: 16px; }
      .grid { grid-template-columns: 1fr; gap: 10px; }
      .hero-card, .metric-card, .ac-card, .climate-card, .chart-card, .events { grid-column: 1; }
      .card { border-radius: 20px; padding: 18px; }
      .status-bar { align-items: flex-start; flex-direction: column; }
      .settings-link { font-size: 0; }
      .settings-link::after { content: "Settings"; font-size: 14px; }
      .chart-tools { align-items: flex-start; flex-direction: column; }
      .status-bar { flex-wrap: wrap; }
      .status-spacer { display: none; }
      .events-heading { align-items: flex-start; flex-direction: column; }
      .temperature-control { grid-template-columns: 50px 1fr 50px; }
      .step-button { width: 50px; height: 50px; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div class="brand">
      <div class="mark">AIR</div>
      <div><h1>LocalAIR</h1><div class="tagline">Local climate control</div></div>
    </div>
    <div class="header-actions">
      <button id="themeToggle" class="quiet-button" type="button" aria-pressed="false" onclick="toggleTheme()">Dark mode</button>
      <a class="settings-link" href="/settings">Settings</a>
    </div>
  </header>

  <section class="status-bar">
    <div><span id="dot" class="dot"></span> <strong id="status">Starting</strong> <span id="detail" class="muted"></span></div>
    <span class="status-spacer"></span>
    <div id="timezone" class="muted">Local time</div>
    <div id="updated" class="muted">Updated —</div>
  </section>

  <section class="grid">
    <div class="card hero-card">
      <div class="eyebrow">Current climate</div>
      <h2 id="heroTitle" class="hero-title">Starting LocalAIR</h2>
      <p id="heroSummary" class="hero-summary">Waiting for a room reading</p>
      <p id="heroDetail" class="hero-detail">Climate schedule loading</p>
    </div>
    <div class="card metric-card">
      <div class="eyebrow">Room temperature</div>
      <div id="temperature" class="metric temp">--.-°C</div>
      <div class="muted">Current room temperature</div>
    </div>
    <div class="card metric-card">
      <div class="eyebrow">Relative humidity</div>
      <div id="humidity" class="metric humidity">--%</div>
      <div class="muted">Current room humidity</div>
    </div>

    <div class="card ac-card">
      <div class="card-heading">
        <div><div class="eyebrow">Olimpia Splendid</div><h2>PIU14 controls</h2></div>
        <span id="powerState" class="state-pill">Unknown</span>
      </div>
      <div class="controls">
        <div class="power-control">
          <div><div class="eyebrow">Power</div><p class="muted">Cloud-connected air conditioner</p></div>
          <button id="powerButton" class="power-button" onclick="sendIR('power')">Turn on</button>
        </div>
        <div class="temperature-control">
          <button id="tempDown" class="step-button" aria-label="Decrease target temperature" onclick="sendIR('temp_down')">−</button>
          <div class="setpoint"><strong id="setpoint">--°</strong><span>Target temperature · 16–30 °C</span></div>
          <button id="tempUp" class="step-button" aria-label="Increase target temperature" onclick="sendIR('temp_up')">+</button>
        </div>
      </div>
      <p id="manualHint" class="manual-hint muted"></p>
      <p id="lastResult" class="feedback" aria-live="polite"></p>
      <div class="sync-local">
        <div><strong>Correct assumed state</strong><div class="muted">Local only — does not send an AC command</div></div>
        <div class="sync-actions">
          <button type="button" onclick="syncPower('on')">Set as On</button>
          <button type="button" onclick="syncPower('off')">Set as Off</button>
        </div>
      </div>
    </div>

    <div class="card climate-card">
      <div class="card-heading">
        <div><div class="eyebrow">Automatic control</div><h2>Climate control</h2></div>
        <button id="climateToggle" class="toggle" type="button" aria-label="Toggle climate control" aria-pressed="false" onclick="toggleClimate()"></button>
      </div>
      <div class="automation-panel">
        <div id="automationState" class="automation-state">
          <span class="automation-dot"></span>
          <div>
            <div class="eyebrow">Climate control</div>
            <div id="automationTitle" class="automation-title">Loading</div>
            <div id="automationDetail" class="automation-detail"></div>
          </div>
        </div>
        <div class="automation-values">
          <div class="automation-value"><span>Schedule</span><strong id="scheduleValue">--</strong></div>
          <div class="automation-value"><span>Turn on at</span><strong id="onValue">--</strong></div>
          <div class="automation-value"><span>Turn off at</span><strong id="offValue">--</strong></div>
          <div class="automation-value"><span>AC setting</span><strong id="climateSetpoint">--</strong></div>
          <div class="automation-value next"><span>Next action</span><strong id="nextAction">--</strong></div>
        </div>
      </div>
    </div>

    <div class="card chart-card">
      <div class="chart-tools">
        <h2>History</h2>
        <div class="chart-actions">
          <div class="legend">
            <span><i class="temp-line"></i>Temperature</span>
            <span><i class="humidity-line"></i>Humidity</span>
          </div>
          <select id="range" onchange="loadHistory()">
            <option value="24h" selected>24 hours</option>
            <option value="7d">7 days</option>
            <option value="30d">30 days</option>
          </select>
        </div>
      </div>
      <div class="chart-wrap">
        <canvas id="chart" width="1100" height="320" aria-label="Temperature, humidity, and AC activity history"></canvas>
        <div id="chartTooltip" class="chart-tooltip" role="tooltip" hidden></div>
      </div>
    </div>

    <div class="card events">
      <div class="events-heading">
        <h2>Recent activity</h2>
        <div class="event-filters" aria-label="Activity filters">
          <button class="filter active" data-filter="all" onclick="setEventFilter('all')">All events</button>
          <button class="filter" data-filter="automatic" onclick="setEventFilter('automatic')">Automatic</button>
          <button class="filter" data-filter="manual" onclick="setEventFilter('manual')">Manual</button>
          <button class="filter" data-filter="errors" onclick="setEventFilter('errors')">Errors</button>
        </div>
      </div>
      <div id="events" class="muted">No events yet.</div>
    </div>
  </section>
</main>

<script>
const labels = {
  power: "Power", temp_down: "Temp −", temp_up: "Temp +",
  power_on: "Power turned on", power_off: "Power turned off",
  climate_on: "Cooling started", climate_off: "Cooling stopped"
};
let latestStatus = null;
let latestEvents = [];
let eventFilter = "all";
let chartMarkers = [];

// Convert stored state values into compact UI labels.
function title(value) {
  if (!value) return "Unknown";
  return String(value).charAt(0).toUpperCase() + String(value).slice(1);
}

// Present activity sources consistently.
function sourceTitle(value) {
  return value === "localair" ? "LocalAIR" : title(value);
}

// Escape activity text before inserting it into HTML.
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

// Format event timestamps in the viewer's locale.
function formatDateTime(value) {
  return new Date(value).toLocaleString([], {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  });
}

// Format a configured HH:MM value in the viewer's locale.
function formatClock(value) {
  const [hour, minute] = value.split(":").map(Number);
  const date = new Date();
  date.setHours(hour, minute, 0, 0);
  return date.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
}

// Turn stored device and control events into readable activity text.
function eventText(event) {
  if (event.kind === "device") {
    const detail = event.error_message ? ` — ${event.error_message}` : "";
    return `Device · ${event.status || "Status"}${detail}`;
  }
  const command = event.command?.startsWith("temperature_")
    ? `Target set to ${event.command.split("_")[1]}°C`
    : event.command ? labels[event.command] || title(event.command) : event.nec_hex || "Unknown IR";
  return `${sourceTitle(event.source)} AC · ${command}`;
}

// Classify activity for dashboard filters and marker colors.
function eventCategory(event) {
  if (event.kind === "device") return "errors";
  return event.source === "climate" ? "automatic" : "manual";
}

// Render the selected recent-activity category.
function renderEvents() {
  const events = latestEvents.filter(event =>
    eventFilter === "all" || eventCategory(event) === eventFilter
  );
  document.getElementById("events").innerHTML = events.length
    ? events.map(event => {
        const category = eventCategory(event);
        return `<div class="event"><span class="event-main"><i class="event-marker ${category}"></i>${escapeHtml(eventText(event))}</span><span>${formatDateTime(event.timestamp)}</span></div>`;
      }).join("")
    : "No matching activity.";
}

// Select an activity filter without fetching the status again.
function setEventFilter(filter) {
  eventFilter = filter;
  document.querySelectorAll(".filter").forEach(button =>
    button.classList.toggle("active", button.dataset.filter === filter)
  );
  renderEvents();
}

// Refresh readings, controls, and activity from the current service snapshot.
async function refreshStatus() {
  const data = await fetch("/api/status").then(r => r.json());
  latestStatus = data;
  document.getElementById("status").textContent = data.reading.status;
  document.getElementById("detail").textContent = `· ${data.device.ip_address}`;
  document.getElementById("timezone").textContent = data.device.timezone;
  document.getElementById("updated").textContent = `Updated ${new Date(data.reading.timestamp).toLocaleTimeString()}`;
  const dot = document.getElementById("dot");
  dot.className = data.reading.ok ? "dot ok" : (data.reading.status === "Error" ? "dot error" : "dot");

  document.getElementById("temperature").textContent = data.reading.temperature_c == null ? "--.-°C" : `${data.reading.temperature_c.toFixed(1)}°C`;
  document.getElementById("humidity").textContent = data.reading.humidity_percent == null ? "--%" : `${data.reading.humidity_percent}%`;

  const room = data.reading.temperature_c == null
    ? "Room temperature unavailable"
    : `Room ${data.reading.temperature_c.toFixed(1)}°C · Target band ${data.climate.turn_off_at}–${data.climate.turn_on_at}°C`;
  const heroTitle = !data.reading.ok
    ? "Sensor unavailable"
    : !data.climate.enabled
      ? "Manual control"
      : data.climate.status === "Cooling"
        ? "Cooling now"
        : data.climate.status;
  document.getElementById("heroTitle").textContent = heroTitle;
  document.getElementById("heroSummary").textContent = room;
  document.getElementById("heroDetail").textContent = data.climate.enabled
    ? data.climate.status === "Outside schedule"
      ? `Scheduled ${formatClock(data.climate.start_time)}–${formatClock(data.climate.end_time)}`
      : `Scheduled until ${formatClock(data.climate.end_time)}`
    : "Automatic climate control is disabled";

  const power = data.ac_state.power || "unknown";
  const powerBox = document.getElementById("powerState");
  powerBox.className = `state-pill ${power}`;
  powerBox.textContent = power === "unknown" ? "Unknown" : `Power ${power}`;
  document.getElementById("setpoint").textContent = data.ac_state.temperature
    ? `${data.ac_state.temperature}°`
    : "--°";

  const powerButton = document.getElementById("powerButton");
  powerButton.textContent = power === "on" ? "Turn off" : "Turn on";
  powerButton.className = `power-button ${power}`;
  powerButton.disabled = !data.ir_commands.power;
  document.getElementById("tempDown").disabled = !data.ir_commands.temp_down;
  document.getElementById("tempUp").disabled = !data.ir_commands.temp_up;
  document.getElementById("manualHint").textContent = data.climate.enabled
    ? "Climate control may override manual changes at the next threshold."
    : "Manual controls are available.";

  const climateToggle = document.getElementById("climateToggle");
  climateToggle.classList.toggle("on", data.climate.enabled);
  climateToggle.setAttribute("aria-pressed", data.climate.enabled);
  climateToggle.setAttribute("aria-label", `${data.climate.enabled ? "Disable" : "Enable"} climate control`);

  const climateClass = data.climate.status.toLowerCase().replaceAll(" ", "-");
  document.getElementById("automationState").className = `automation-state ${climateClass}`;
  document.getElementById("automationTitle").textContent = data.climate.status;
  document.getElementById("automationDetail").textContent = data.climate.detail;
  document.getElementById("scheduleValue").textContent =
    `${formatClock(data.climate.start_time)}–${formatClock(data.climate.end_time)}`;
  document.getElementById("onValue").textContent = `${data.climate.turn_on_at}°C`;
  document.getElementById("offValue").textContent = `${data.climate.turn_off_at}°C`;
  document.getElementById("climateSetpoint").textContent =
    `${data.climate.aircon_temperature}°C`;
  document.getElementById("nextAction").textContent = !data.climate.enabled
    ? "Enable automation to begin"
    : data.climate.status === "Cooling"
      ? `Cooling stops at ${data.climate.turn_off_at}°C`
      : data.climate.status === "Outside schedule"
        ? `Schedule starts at ${formatClock(data.climate.start_time)}`
        : data.climate.status === "Sensor unavailable"
          ? "Waiting for a valid sensor reading"
          : `Cooling starts at ${data.climate.turn_on_at}°C`;

  const feedback = document.getElementById("lastResult");
  feedback.textContent = data.last_result ? data.last_result.message : "";
  feedback.className = `feedback ${data.last_result ? (data.last_result.ok ? "success" : "error") : ""}`;
  latestEvents = data.events;
  renderEvents();
}

// Send one PIU14 command and expose progress or failure immediately.
async function sendIR(command) {
  const button = document.getElementById({power: "powerButton", temp_down: "tempDown", temp_up: "tempUp"}[command]);
  const feedback = document.getElementById("lastResult");
  button.disabled = true;
  feedback.className = "feedback";
  feedback.textContent = command === "power" ? "Sending power command…" : "Changing temperature…";
  try {
    const response = await fetch(`/api/ir/${command}`, {method: "POST"});
    const result = await response.json();
    feedback.className = `feedback ${response.ok ? "success" : "error"}`;
    feedback.textContent = result.message || result.detail || "Command failed.";
  } catch (_error) {
    feedback.className = "feedback error";
    feedback.textContent = "LocalAIR could not be reached.";
  }
  await refreshStatus();
}

// Set the locally assumed power state without operating the AC.
async function syncPower(power) {
  await fetch(`/api/sync/power/${power}`, {method: "POST"});
  await refreshStatus();
}

// Enable or disable climate automation from the dashboard.
async function toggleClimate() {
  const enabled = latestStatus?.climate.enabled;
  const toggle = document.getElementById("climateToggle");
  toggle.disabled = true;
  await fetch(`/api/climate/${enabled ? "off" : "on"}`, {method: "POST"});
  await refreshStatus();
  toggle.disabled = false;
}

// Load the selected history window and redraw its chart.
async function loadHistory() {
  const range = document.getElementById("range").value;
  const data = await fetch(`/api/history?range=${range}`).then(r => r.json());
  drawChart(data.points, range, data.events);
}

// Toggle and persist the light or dark appearance.
function toggleTheme() {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("theme", theme);
  document.getElementById("themeToggle").textContent = theme === "dark" ? "Light mode" : "Dark mode";
  document.getElementById("themeToggle").setAttribute("aria-pressed", theme === "dark");
  loadHistory();
}

// Render responsive readings, thresholds, and AC activity markers.
function drawChart(points, range, events = []) {
  const canvas = document.getElementById("chart");
  const w = canvas.clientWidth || 1100;
  const h = canvas.clientHeight || 270;
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  chartMarkers = [];
  ctx.clearRect(0, 0, w, h);
  const styles = getComputedStyle(document.documentElement);
  const muted = styles.getPropertyValue("--muted").trim();
  const gridLine = styles.getPropertyValue("--grid-line").trim();
  const orange = styles.getPropertyValue("--orange").trim();
  const blue = styles.getPropertyValue("--blue").trim();
  const left = 58, right = 58, top = 24, bottom = 42;
  const gw = w - left - right, gh = h - top - bottom;

  if (points.length === 0) {
    ctx.fillStyle = muted; ctx.font = "15px sans-serif";
    ctx.fillText("History graph appears after the first stored reading", left + 245, top + gh / 2);
    return;
  }

  const temps = points.map(p => p.temperature_c);
  const humidity = points.map(p => p.humidity_percent);
  const thresholds = latestStatus
    ? [latestStatus.climate.turn_off_at, latestStatus.climate.turn_on_at]
    : [];
  const scaleTemps = [...temps, ...thresholds];
  const tempScale = axisScale(Math.min(...scaleTemps), Math.max(...scaleTemps), 1, "temperature");
  const humidityScale = axisScale(Math.min(...humidity), Math.max(...humidity), 10, "humidity");
  const dates = points.map(p => new Date(p.timestamp));
  const endTime = Date.now();
  const duration = {"24h": 86400000, "7d": 604800000, "30d": 2592000000}[range] || 86400000;
  const startTime = endTime - duration;
  const timeSpan = Math.max(1, endTime - startTime);

  drawAxes();

  if (points.length === 1) {
    drawPoint(points[0].temperature_c, tempScale.min, tempScale.max, orange, left + gw / 2);
    drawPoint(points[0].humidity_percent, humidityScale.min, humidityScale.max, blue, left + gw / 2 + 12);
  } else {
    drawLine(points.map(p => p.temperature_c), tempScale.min, tempScale.max, orange, false);
    drawLine(points.map(p => p.humidity_percent), humidityScale.min, humidityScale.max, blue, false);
  }
  drawEventMarkers(events);

  // Calculate a readable axis range for one metric.
  function axisScale(minValue, maxValue, preferredStep, kind) {
    let min = kind === "temperature"
      ? Math.floor(minValue)
      : Math.max(0, Math.floor(minValue / preferredStep) * preferredStep);
    let max = kind === "temperature"
      ? Math.ceil(maxValue)
      : Math.min(100, Math.ceil(maxValue / preferredStep) * preferredStep);
    if (max === min) max = min + preferredStep;

    let step = preferredStep;
    while ((max - min) / step > 8) step *= 2;
    if (kind === "temperature" && (max - min) < 4) {
      const mid = (min + max) / 2;
      min = Math.floor(mid - 2);
      max = Math.ceil(mid + 2);
      step = 1;
    }

    const ticks = [];
    for (let value = min; value <= max + 0.001; value += step) {
      ticks.push(value);
    }
    return {min, max, ticks};
  }

  // Draw grid lines, units, and time labels for both metrics.
  function drawAxes() {
    ctx.font = "12px sans-serif";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 1;

    ctx.textBaseline = "top";
    ctx.font = "12px sans-serif";
    ctx.fillStyle = orange;
    ctx.textAlign = "right";
    ctx.fillText("°C", left - 8, 4);
    ctx.fillStyle = blue;
    ctx.textAlign = "left";
    ctx.fillText("%", left + gw + 8, 4);
    ctx.textBaseline = "middle";

    for (const value of tempScale.ticks) {
      const y = yFor(value, tempScale.min, tempScale.max);
      ctx.strokeStyle = gridLine;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(left + gw, y);
      ctx.stroke();
      ctx.fillStyle = orange;
      ctx.textAlign = "right";
      ctx.fillText(`${value}°`, left - 8, y);
    }

    for (const value of humidityScale.ticks) {
      const y = yFor(value, humidityScale.min, humidityScale.max);
      ctx.fillStyle = blue;
      ctx.textAlign = "left";
      ctx.fillText(`${value}%`, left + gw + 8, y);
    }

    ctx.textBaseline = "top";
    ctx.fillStyle = muted;
    ctx.strokeStyle = gridLine;
    if (latestStatus) {
      drawThreshold(latestStatus.climate.turn_on_at, "Cooling starts", orange);
      drawThreshold(latestStatus.climate.turn_off_at, "Cooling stops", blue);
    }

    for (const tick of timeTicks(range, new Date(startTime), new Date(endTime))) {
      const x = xFor(tick.date);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, top + gh);
      ctx.stroke();
      ctx.textAlign = tick.align || "center";
      ctx.fillText(tick.label, x, top + gh + 10);
    }
  }

  // Draw one configured climate boundary across the temperature plot.
  function drawThreshold(value, label, color) {
    const y = yFor(value, tempScale.min, tempScale.max);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.globalAlpha = .65;
    ctx.setLineDash([6, 6]);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + gw, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    ctx.fillStyle = color;
    ctx.textAlign = "left";
    ctx.fillText(`${value}°C  ${label}`, left + 8, y - 10);
    ctx.restore();
  }

  // Choose evenly spaced time labels for the selected history window.
  function timeTicks(range, start, end) {
    const ticks = [];
    const startMs = start.getTime(), endMs = end.getTime();
    const stepMinutes = { "24h": 180, "7d": 1440, "30d": 10080 }[range] || 180;
    const stepMs = stepMinutes * 60 * 1000;
    const first = Math.ceil(startMs / stepMs) * stepMs;

    ticks.push({date: start, label: timeLabel(start, range), align: "left"});
    for (let ms = first; ms < endMs; ms += stepMs) {
      if (ms <= startMs + 60000 || ms >= endMs - 60000) continue;
      const date = new Date(ms);
      ticks.push({date, label: timeLabel(date, range)});
    }
    if (endMs - startMs > 60000) {
      ticks.push({date: end, label: timeLabel(end, range), align: "right"});
    }
    return ticks;
  }

  // Format a chart time label for short or long ranges.
  function timeLabel(date, range) {
    if (range === "7d" || range === "30d") {
      return date.toLocaleDateString([], {month: "short", day: "numeric"});
    }
    return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }

  // Map a timestamp into the graph's horizontal plot area.
  function xFor(date) {
    return left + gw * ((date.getTime() - startTime) / timeSpan);
  }

  // Map a metric value into the graph's vertical plot area.
  function yFor(value, min, max) {
    return top + gh * (1 - (value - min) / (max - min));
  }

  // Draw a rounded quadratic curve through all readings.
  function drawLine(values, min, max, color, dashed) {
    const coordinates = values.map((value, i) => ({
      x: xFor(dates[i]), y: yFor(value, min, max)
    }));
    ctx.strokeStyle = color;
    ctx.lineWidth = 3.5;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.setLineDash(dashed ? [8, 7] : []);
    ctx.beginPath();
    ctx.moveTo(coordinates[0].x, coordinates[0].y);
    for (let i = 1; i < coordinates.length - 1; i++) {
      const midpointX = (coordinates[i].x + coordinates[i + 1].x) / 2;
      const midpointY = (coordinates[i].y + coordinates[i + 1].y) / 2;
      ctx.quadraticCurveTo(
        coordinates[i].x, coordinates[i].y, midpointX, midpointY
      );
    }
    const last = coordinates[coordinates.length - 1];
    ctx.quadraticCurveTo(last.x, last.y, last.x, last.y);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // Draw one reading when a history window has a single sample.
  function drawPoint(value, min, max, color, x) {
    const y = yFor(value, min, max);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fill();
  }

  // Place successful automatic and manual AC actions on the temperature line.
  function drawEventMarkers(activity) {
    chartMarkers = [];
    for (const event of activity) {
      const eventDate = new Date(event.timestamp);
      const eventTime = eventDate.getTime();
      if (eventTime < startTime || eventTime > endTime) continue;
      let nearest = 0;
      for (let i = 1; i < dates.length; i++) {
        if (Math.abs(dates[i] - eventTime) < Math.abs(dates[nearest] - eventTime)) nearest = i;
      }
      const x = xFor(eventDate);
      const y = yFor(points[nearest].temperature_c, tempScale.min, tempScale.max);
      const color = event.source === "climate" ? blue : orange;
      ctx.fillStyle = "white";
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(x, y, 5.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      chartMarkers.push({x, y, event, temperature: points[nearest].temperature_c});
    }
  }
}

// Show details for the closest AC action marker on hover or touch.
function showChartTooltip(pointer) {
  const canvas = document.getElementById("chart");
  const tooltip = document.getElementById("chartTooltip");
  const rect = canvas.getBoundingClientRect();
  const x = pointer.clientX - rect.left;
  const y = pointer.clientY - rect.top;
  const marker = chartMarkers.find(item => Math.hypot(item.x - x, item.y - y) <= 12);
  if (!marker) {
    tooltip.hidden = true;
    canvas.style.cursor = "default";
    return;
  }
  const action = marker.event.command?.startsWith("temperature_")
    ? `Target set to ${marker.event.command.split("_")[1]}°C`
    : labels[marker.event.command] || title(marker.event.command);
  const source = marker.event.source === "climate" ? "Automatic climate control" : "Manual control";
  tooltip.innerHTML = `<strong>${escapeHtml(action)}</strong>${marker.temperature.toFixed(1)}°C<br>${new Date(marker.event.timestamp).toLocaleTimeString()}<br>${source}`;
  tooltip.style.left = `${Math.max(100, Math.min(rect.width - 100, marker.x))}px`;
  tooltip.style.top = `${marker.y}px`;
  tooltip.hidden = false;
  canvas.style.cursor = "pointer";
}

document.getElementById("themeToggle").textContent =
  document.documentElement.dataset.theme === "dark" ? "Light mode" : "Dark mode";
document.getElementById("themeToggle").setAttribute(
  "aria-pressed", document.documentElement.dataset.theme === "dark"
);
document.getElementById("chart").addEventListener("pointermove", showChartTooltip);
document.getElementById("chart").addEventListener("pointerleave", () => {
  document.getElementById("chartTooltip").hidden = true;
});
refreshStatus();
loadHistory();
setInterval(refreshStatus, 3000);
setInterval(loadHistory, 30000);
</script>
</body>
</html>
"""


SETTINGS_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LocalAIR Settings</title>
  <link rel="icon" type="image/png" href="/localair.png">
  <link rel="apple-touch-icon" href="/localair.png">
  <script>
    document.documentElement.dataset.theme = localStorage.getItem("theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  </script>
  <style>
    :root {{ color-scheme: light; --bg: #f4f7fb; --card: rgba(255,255,255,.86); --surface: #f8fafc; --text: #111827; --muted: #64748b; --line: #dce4ee; --blue: #3578f6; --shadow: 0 24px 70px rgba(43,58,85,.11); }}
    :root[data-theme="dark"] {{ color-scheme: dark; --bg: #090d14; --card: rgba(20,27,39,.88); --surface: #111827; --text: #f1f5f9; --muted: #94a3b8; --line: #2a3546; --blue: #66a0ff; --shadow: 0 24px 70px rgba(0,0,0,.35); }}
    * {{ box-sizing: border-box; }}
    body {{ min-height: 100vh; margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at 8% 0%, rgba(53,120,246,.16), transparent 32rem), var(--bg); color: var(--text); }}
    main {{ width: min(820px, calc(100% - 28px)); margin: 0 auto; padding: 28px 0 42px; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }}
    h1 {{ margin: 0; font-size: 28px; letter-spacing: -.04em; }}
    h2 {{ grid-column: 1 / -1; margin: 12px 0 0; font-size: 13px; color: var(--muted); letter-spacing: .08em; text-transform: uppercase; }}
    a {{ padding: 9px 13px; border: 1px solid var(--line); border-radius: 12px; color: var(--text); background: var(--card); text-decoration: none; font-weight: 750; }}
    .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 24px; padding: 24px; box-shadow: var(--shadow); backdrop-filter: blur(18px); }}
    .form-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .field {{ display: grid; gap: 7px; }}
    .wide {{ grid-column: 1 / -1; }}
    label {{ font-size: 13px; font-weight: 800; }}
    input {{ width: 100%; padding: 12px 13px; border: 1px solid var(--line); border-radius: 12px; outline: none; color: var(--text); background: var(--surface); font: inherit; }}
    input:focus {{ border-color: var(--blue); box-shadow: 0 0 0 3px color-mix(in srgb, var(--blue) 18%, transparent); }}
    .toggle-row {{ grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 16px; border: 1px solid var(--line); border-radius: 16px; background: var(--surface); }}
    .toggle-row strong, .toggle-row span {{ display: block; }}
    .toggle-row span {{ margin-top: 3px; color: var(--muted); font-size: 13px; font-weight: 500; }}
    .toggle-row input {{ width: 22px; height: 22px; flex: 0 0 auto; accent-color: var(--blue); }}
    .actions {{ grid-column: 1 / -1; display: flex; align-items: center; gap: 10px; margin-top: 8px; }}
    button {{ border: 0; border-radius: 12px; padding: 11px 16px; color: white; background: linear-gradient(145deg, #2469ed, #438cff); font: inherit; font-weight: 800; cursor: pointer; }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 620px) {{ .form-grid {{ grid-template-columns: 1fr; }} .wide, h2, .actions {{ grid-column: 1; }} .card {{ padding: 18px; border-radius: 20px; }} }}
  </style>
</head>
<body>
<main>
  <header><div><h1>Settings</h1><div class="muted">LocalAIR and PIU14 connection details</div></div><a href="/">Back</a></header>
  <div class="card">
    <p class="muted">Keys and secrets are saved to config only and are not printed by LocalAIR.</p>
    <form class="form-grid" method="post">
      <h2>Local sensor</h2>
      <div class="field"><label>IP address</label><input name="ip_address" value="{ip_address}" required></div>
      <div class="field"><label>Device ID</label><input name="device_id" value="{device_id}" required></div>
      <div class="field"><label>Local key</label><input name="local_key" type="password" value="" placeholder="Leave blank to keep current key"></div>
      <div class="field"><label>Protocol version</label><input name="protocol_version" value="{protocol_version}" required></div>
      <div class="field wide"><label>Poll interval seconds</label><input name="poll_interval_seconds" type="number" min="5" max="3600" value="{poll_interval_seconds}" required></div>
      <h2>Olimpia Splendid PIU14</h2>
      <div class="field"><label>Tuya cloud region</label><input name="cloud_region" value="{cloud_region}" required></div>
      <div class="field"><label>Virtual AC device ID</label><input name="aircon_device_id" value="{aircon_device_id}" required></div>
      <div class="field"><label>Tuya cloud API key</label><input name="cloud_api_key" type="password" value="" placeholder="Leave blank to keep current key"></div>
      <div class="field"><label>Tuya cloud API secret</label><input name="cloud_api_secret" type="password" value="" placeholder="Leave blank to keep current secret"></div>
      <h2>Climate schedule</h2>
      <label class="toggle-row"><span><strong>Enable climate control</strong><span>Automatically cool only during the configured daily schedule.</span></span><input name="climate_enabled" type="checkbox" {climate_enabled}></label>
      <div class="field"><label>Start time</label><input name="climate_start" type="time" value="{climate_start}" required></div>
      <div class="field"><label>End time</label><input name="climate_end" type="time" value="{climate_end}" required></div>
      <div class="field"><label>Turn cooling on at</label><input name="turn_on_at" type="number" min="-20" max="60" step="0.1" value="{turn_on_at}" required></div>
      <div class="field"><label>Turn cooling off at</label><input name="turn_off_at" type="number" min="-20" max="60" step="0.1" value="{turn_off_at}" required></div>
      <div class="field"><label>Air-conditioner temperature</label><input name="aircon_temperature" type="number" min="16" max="30" value="{aircon_temperature}" required></div>
      <div class="field"><label>Sensor timeout seconds</label><input name="sensor_timeout_seconds" type="number" min="5" max="86400" value="{sensor_timeout_seconds}" required></div>
      <h2>LocalAIR service</h2>
      <div class="field"><label>Web host</label><input name="server_host" value="{server_host}" required></div>
      <div class="field"><label>Web port</label><input name="server_port" type="number" min="1" max="65535" value="{server_port}" required></div>
      <div class="field wide"><label>History retention days</label><input name="history_retention_days" type="number" min="1" max="365" value="{history_retention_days}" required></div>
      <div class="actions"><button type="submit">Save settings</button><a href="/">Cancel</a></div>
    </form>
  </div>
</main>
</body>
</html>
"""
