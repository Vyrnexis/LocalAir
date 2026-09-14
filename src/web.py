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
            "events": self.store.recent_activity_events(),
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

    def sync_power(self) -> dict[str, str]:
        with self.lock:
            current = self.config.ac_state.get("power")
            next_power = "off" if current == "on" else "on"
            new_state = dict(self.config.ac_state)
            new_state["power"] = next_power
        self._save_state(new_state, "sync")
        return {"power": next_power}

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
        self.store.add_ir_event(source, command)
        with self.lock:
            config = self.config
            state = update_ac_state(config.ac_state, command)
            if command == "climate_on":
                state["temperature"] = str(config.aircon_temperature)
        self._save_state(state, f"ir:{command}")
        with self.lock:
            self.last_ir = (
                command,
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
    return JSONResponse({"points": get_service().store.history(range)})


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
    main { width: min(1160px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 42px; }
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
    .status-bar {
      padding: 0 2px; margin-bottom: 14px;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      color: var(--muted); font-size: 14px;
    }
    .dot { width: 9px; height: 9px; border-radius: 99px; background: #94a3b8; display: inline-block; }
    .dot.ok { background: var(--green); }
    .dot.error { background: var(--red); }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .card {
      background: var(--card); border: 1px solid var(--line);
      border-radius: 24px; padding: 22px; box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }
    .card h2 { margin: 0; font-size: 16px; letter-spacing: -.02em; }
    .eyebrow { color: var(--muted); font-size: 12px; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; }
    .metric-card { min-height: 180px; display: flex; flex-direction: column; justify-content: space-between; }
    .metric { font-size: clamp(48px, 7vw, 76px); line-height: .9; font-weight: 820; letter-spacing: -.07em; }
    .temp { color: var(--orange); }
    .humidity { color: var(--blue); }
    .muted { color: var(--muted); }
    .climate-card { grid-column: 1 / -1; display: grid; gap: 22px; }
    .card-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .state-pill { padding: 6px 10px; border-radius: 999px; background: var(--surface); color: var(--muted); font-size: 13px; font-weight: 800; }
    .state-pill.on { color: var(--green); background: color-mix(in srgb, var(--green) 13%, transparent); }
    .controls { display: grid; grid-template-columns: .75fr 1.25fr; gap: 16px; }
    .power-control, .temperature-control { min-height: 170px; border-radius: 20px; background: var(--surface); padding: 18px; }
    .power-control { display: grid; gap: 12px; }
    .temperature-control { display: grid; grid-template-columns: 56px 1fr 56px; align-items: center; gap: 12px; }
    .setpoint { text-align: center; }
    .setpoint strong { display: block; font-size: clamp(48px, 7vw, 72px); line-height: .95; letter-spacing: -.07em; }
    .setpoint span { display: block; margin-top: 8px; color: var(--muted); font-size: 13px; }
    .automation-panel {
      display: grid; grid-template-columns: 1.2fr 1fr; gap: 18px;
      padding: 18px; border: 1px solid var(--line); border-radius: 20px;
      background: color-mix(in srgb, var(--blue) 5%, var(--surface));
    }
    .automation-state { display: flex; align-items: flex-start; gap: 12px; }
    .automation-dot { width: 11px; height: 11px; margin-top: 5px; border-radius: 50%; background: var(--muted); box-shadow: 0 0 0 5px color-mix(in srgb, var(--muted) 12%, transparent); }
    .automation-state.cooling .automation-dot { background: var(--green); box-shadow: 0 0 0 5px color-mix(in srgb, var(--green) 14%, transparent); }
    .automation-state.sensor-unavailable .automation-dot { background: var(--red); box-shadow: 0 0 0 5px color-mix(in srgb, var(--red) 14%, transparent); }
    .automation-title { font-size: 20px; font-weight: 850; letter-spacing: -.03em; }
    .automation-detail { margin-top: 4px; color: var(--muted); font-size: 14px; }
    .automation-values { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
    .automation-value { padding: 10px; border-radius: 14px; background: var(--card); }
    .automation-value span { display: block; color: var(--muted); font-size: 11px; font-weight: 800; letter-spacing: .05em; text-transform: uppercase; }
    .automation-value strong { display: block; margin-top: 4px; font-size: 17px; }
    .manual-hint { margin: 0; font-size: 13px; }
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
    canvas { width: 100%; height: 280px; display: block; }
    .events { grid-column: 1 / -1; }
    .events h2 { margin-bottom: 12px; }
    .event { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--line); }
    .event span:last-child { color: var(--muted); white-space: nowrap; }
    @media (max-width: 820px) {
      .controls, .automation-panel { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      main { width: min(100% - 20px, 1160px); padding-top: 16px; }
      .grid { grid-template-columns: 1fr; gap: 10px; }
      .metric-card, .climate-card, .chart-card, .events { grid-column: 1; }
      .card { border-radius: 20px; padding: 18px; }
      .status-bar { align-items: flex-start; flex-direction: column; }
      .settings-link { font-size: 0; }
      .settings-link::after { content: "Settings"; font-size: 14px; }
      .chart-tools { align-items: flex-start; flex-direction: column; }
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
    <div id="updated" class="muted">Last update —</div>
  </section>

  <section class="grid">
    <div class="card metric-card">
      <div class="eyebrow">Room temperature</div>
      <div id="temperature" class="metric temp">--.-°C</div>
      <div class="muted">Measured by the Tuya sensor</div>
    </div>
    <div class="card metric-card">
      <div class="eyebrow">Relative humidity</div>
      <div id="humidity" class="metric humidity">--%</div>
      <div class="muted">Current room humidity</div>
    </div>

    <div class="card climate-card">
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
          <div class="automation-value"><span>Cooling band</span><strong id="bandValue">--</strong></div>
          <div class="automation-value"><span>AC setting</span><strong id="climateSetpoint">--</strong></div>
        </div>
      </div>
      <p id="manualHint" class="manual-hint muted"></p>
      <p id="lastResult" class="muted" aria-live="polite"></p>
      <button class="sync" title="Correct LocalAIR's power state without sending a command" onclick="syncPower()">Power state incorrect? Sync it locally</button>
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
            <option value="24h" selected>Daily</option>
            <option value="7d">Weekly</option>
            <option value="31d">Monthly</option>
          </select>
        </div>
      </div>
      <canvas id="chart" width="1100" height="270"></canvas>
    </div>

    <div class="card events">
      <h2>Recent activity</h2>
      <div id="events" class="muted">No events yet.</div>
    </div>
  </section>
</main>

<script>
const labels = {
  power: "Power", temp_down: "Temp −", temp_up: "Temp +",
  climate_on: "Cooling started", climate_off: "Cooling stopped"
};

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

// Turn stored device and control events into readable activity text.
function eventText(event) {
  if (event.kind === "device") {
    const detail = event.error_message ? ` — ${event.error_message}` : "";
    return `Device · ${event.status || "Status"}${detail}`;
  }
  const command = event.command ? labels[event.command] || title(event.command) : event.nec_hex || "Unknown IR";
  return `${sourceTitle(event.source)} AC · ${command}`;
}

// Refresh readings, controls, and activity from the current service snapshot.
async function refreshStatus() {
  const data = await fetch("/api/status").then(r => r.json());
  document.getElementById("status").textContent = data.reading.status;
  document.getElementById("detail").textContent = `· ${data.device.ip_address}`;
  document.getElementById("updated").textContent = `Last update ${new Date(data.reading.timestamp).toLocaleTimeString()}`;
  const dot = document.getElementById("dot");
  dot.className = data.reading.ok ? "dot ok" : (data.reading.status === "Error" ? "dot error" : "dot");

  document.getElementById("temperature").textContent = data.reading.temperature_c == null ? "--.-°C" : `${data.reading.temperature_c.toFixed(1)}°C`;
  document.getElementById("humidity").textContent = data.reading.humidity_percent == null ? "--%" : `${data.reading.humidity_percent}%`;

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
  const manualEnabled = !data.climate.enabled;
  powerButton.disabled = !data.ir_commands.power || !manualEnabled;
  document.getElementById("tempDown").disabled = !data.ir_commands.temp_down || !manualEnabled;
  document.getElementById("tempUp").disabled = !data.ir_commands.temp_up || !manualEnabled;
  document.getElementById("manualHint").textContent = data.climate.enabled
    ? "Manual controls are locked while climate control is enabled."
    : "Manual controls are available while climate control is disabled.";

  const climateClass = data.climate.status.toLowerCase().replaceAll(" ", "-");
  document.getElementById("automationState").className = `automation-state ${climateClass}`;
  document.getElementById("automationTitle").textContent = data.climate.status;
  document.getElementById("automationDetail").textContent = data.climate.detail;
  document.getElementById("scheduleValue").textContent =
    `${data.climate.start_time}–${data.climate.end_time}`;
  document.getElementById("bandValue").textContent =
    `${data.climate.turn_off_at}–${data.climate.turn_on_at}°`;
  document.getElementById("climateSetpoint").textContent =
    `${data.climate.aircon_temperature}°`;

  document.getElementById("lastResult").textContent = data.last_result ? data.last_result.message : "";
  document.getElementById("events").innerHTML = data.events.length
    ? data.events.map(e => `<div class="event"><span>${escapeHtml(eventText(e))}</span><span>${formatDateTime(e.timestamp)}</span></div>`).join("")
    : "No events yet.";
}

// Queue one PIU14 cloud control and then refresh its assumed state.
async function sendIR(command) {
  await fetch(`/api/ir/${command}`, {method: "POST"});
  setTimeout(refreshStatus, 500);
}

// Correct the locally assumed power state without operating the AC.
async function syncPower() {
  await fetch("/api/sync/power", {method: "POST"});
  refreshStatus();
}

// Load the selected history window and redraw its chart.
async function loadHistory() {
  const range = document.getElementById("range").value;
  const data = await fetch(`/api/history?range=${range}`).then(r => r.json());
  drawChart(data.points, range);
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

// Render responsive temperature and humidity history lines.
function drawChart(points, range) {
  const canvas = document.getElementById("chart");
  const w = canvas.clientWidth || 1100;
  const h = canvas.clientHeight || 270;
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
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
  const tempScale = axisScale(Math.min(...temps), Math.max(...temps), 1, "temperature");
  const humidityScale = axisScale(Math.min(...humidity), Math.max(...humidity), 5, "humidity");
  const dates = points.map(p => new Date(p.timestamp));
  const startTime = dates[0].getTime();
  const endTime = dates[dates.length - 1].getTime();
  const timeSpan = Math.max(1, endTime - startTime);

  drawAxes();

  if (points.length === 1) {
    drawPoint(points[0].temperature_c, tempScale.min, tempScale.max, orange, left + gw / 2);
    drawPoint(points[0].humidity_percent, humidityScale.min, humidityScale.max, blue, left + gw / 2 + 12);
  } else {
    drawLine(points.map(p => p.temperature_c), tempScale.min, tempScale.max, orange, false);
    drawLine(points.map(p => p.humidity_percent), humidityScale.min, humidityScale.max, blue, true);
  }

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
    for (const tick of timeTicks(range, dates[0], dates[dates.length - 1])) {
      const x = xFor(tick.date);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, top + gh);
      ctx.stroke();
      ctx.textAlign = tick.align || "center";
      ctx.fillText(tick.label, x, top + gh + 10);
    }
  }

  // Choose evenly spaced time labels for the selected history window.
  function timeTicks(range, start, end) {
    const ticks = [];
    const startMs = start.getTime(), endMs = end.getTime();
    const stepMinutes = { "24h": 180, "7d": 1440, "31d": 10080 }[range] || 180;
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
    if (range === "7d" || range === "31d") {
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
}

document.getElementById("themeToggle").textContent =
  document.documentElement.dataset.theme === "dark" ? "Light mode" : "Dark mode";
document.getElementById("themeToggle").setAttribute(
  "aria-pressed", document.documentElement.dataset.theme === "dark"
);
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
