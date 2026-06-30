"""LocalAIR web service and dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import threading
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
    CommandResult,
    ConnectionStatus,
    DeviceMonitor,
    DeviceReading,
    IR_COMMANDS,
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
        self.monitor = DeviceMonitor(
            self.config,
            self._on_reading,
            self._on_ir,
            self._on_result,
        )

    def start(self) -> None:
        self.monitor.start()
        self.monitor.refresh()

    def stop(self) -> None:
        self.monitor.stop()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            reading = self.latest_reading
            result = self.last_result
            config = self.config
            last_ir = self.last_ir
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
            "ir_commands": {
                name: name in config.ir_commands
                for name in sorted(IR_COMMANDS)
            },
            "last_result": asdict(result) if result else None,
            "last_ir": (
                {"command": last_ir[0], "timestamp": last_ir[1]}
                if last_ir
                else None
            ),
            "events": self.store.recent_ir_events(),
        }

    def send_ir(self, command: str) -> None:
        if command not in IR_COMMANDS:
            raise ValueError("Unknown IR command.")
        with self.lock:
            if command not in self.config.ir_commands:
                raise ValueError("This IR command has not been learned.")
        self.monitor.send(command)

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
        config = AppConfig(
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
            ir_address=current.ir_address,
            ir_commands=current.ir_commands,
            ac_state=current.ac_state,
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
        self.monitor.update_config(config)
        self.monitor.refresh()

    def _on_reading(self, reading: DeviceReading) -> None:
        with self.lock:
            self.latest_reading = reading
        self.store.add_reading(reading)

    def _on_ir(self, command: str, source: str) -> None:
        self.store.add_ir_event(source, command)
        with self.lock:
            state = update_ac_state(self.config.ac_state, command)
        self._save_state(state, f"ir:{command}")
        with self.lock:
            self.last_ir = (
                command,
                datetime.now().astimezone().isoformat(timespec="seconds"),
            )

    def _on_result(self, result: CommandResult) -> None:
        with self.lock:
            self.last_result = result

    def _save_state(self, state: dict[str, str], source: str) -> None:
        with self.lock:
            old_state = dict(self.config.ac_state)
            self.config = AppConfig(
                ip_address=self.config.ip_address,
                device_id=self.config.device_id,
                local_key=self.config.local_key,
                protocol_version=self.config.protocol_version,
                poll_interval_seconds=self.config.poll_interval_seconds,
                ir_address=self.config.ir_address,
                ir_commands=self.config.ir_commands,
                ac_state=state,
                server_host=self.config.server_host,
                server_port=self.config.server_port,
                history_retention_days=self.config.history_retention_days,
            )
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
    )


@app.post("/settings")
async def save_settings(request: Request) -> RedirectResponse:
    body = (await request.body()).decode()
    data = {key: values[-1] for key, values in parse_qs(body).items()}
    get_service().update_settings(data)
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
        get_service().send_ir(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse({"ok": True, "message": "IR command queued."})


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
  <style>
    :root {
      --bg: #eef4f8;
      --card: #ffffff;
      --text: #172033;
      --muted: #667085;
      --line: #dde7ef;
      --blue: #2170d1;
      --orange: #ef6c42;
      --green: #159463;
      --red: #c93b32;
      --shadow: 0 18px 45px rgba(28, 39, 60, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(33, 112, 209, .18), transparent 28rem),
        linear-gradient(180deg, #f7fbff, var(--bg));
      color: var(--text);
    }
    main { width: min(1180px, calc(100% - 32px)); margin: 24px auto; }
    header {
      display: flex; align-items: center; justify-content: space-between;
      gap: 16px; margin-bottom: 18px;
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .mark {
      width: 48px; height: 48px; border-radius: 16px;
      display: grid; place-items: center;
      background: linear-gradient(135deg, var(--blue), #6bd0ff);
      color: white; box-shadow: var(--shadow); font-weight: 900;
    }
    h1 { margin: 0; font-size: 30px; letter-spacing: -.04em; }
    .tagline { margin-top: 2px; color: var(--muted); }
    a { color: var(--blue); text-decoration: none; font-weight: 700; }
    .status-bar {
      background: rgba(255,255,255,.82); backdrop-filter: blur(12px);
      border: 1px solid var(--line); border-radius: 18px;
      padding: 14px 16px; box-shadow: var(--shadow);
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      margin-bottom: 14px;
    }
    .dot { width: 11px; height: 11px; border-radius: 99px; background: #98a2b3; display: inline-block; }
    .dot.ok { background: var(--green); }
    .dot.error { background: var(--red); }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .card {
      background: rgba(255,255,255,.9); border: 1px solid var(--line);
      border-radius: 22px; padding: 18px; box-shadow: var(--shadow);
    }
    .card h2 { margin: 0 0 12px; font-size: 17px; letter-spacing: -.02em; }
    .metric { font-size: clamp(42px, 6vw, 68px); line-height: .95; font-weight: 850; letter-spacing: -.06em; }
    .temp { color: var(--orange); }
    .humidity { color: var(--blue); }
    .muted { color: var(--muted); }
    .ac-state {
      display: grid; grid-template-columns: minmax(140px, .65fr) 1fr;
      gap: 16px; align-items: stretch;
    }
    .power-state {
      border-radius: 20px; padding: 18px; display: grid; place-items: center;
      color: white; background: #7b8794; text-align: center;
    }
    .power-state.on { background: linear-gradient(135deg, var(--green), #53c98f); }
    .power-state.off { background: linear-gradient(135deg, #475467, #98a2b3); }
    .power-state strong { display: block; font-size: 42px; line-height: 1; }
    .chips { display: flex; gap: 8px; flex-wrap: wrap; }
    .chip { background: #f1f6fb; border: 1px solid var(--line); padding: 9px 11px; border-radius: 999px; color: #344054; font-weight: 700; }
    .buttons { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    button {
      border: 0; border-radius: 13px; padding: 11px 10px; font-weight: 800;
      color: #344054; background: #eef4fb; cursor: pointer;
    }
    button.primary { color: white; background: linear-gradient(135deg, var(--blue), #3d8bff); }
    button.sync { color: #344054; background: #fff7ed; border: 1px solid #fed7aa; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .chart-card { grid-column: 1 / -1; }
    .chart-tools { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    select { border: 1px solid var(--line); border-radius: 12px; padding: 8px 10px; background: white; }
    canvas { width: 100%; height: 270px; display: block; }
    .events { grid-column: 1 / -1; }
    .event { display: flex; justify-content: space-between; gap: 12px; padding: 9px 0; border-bottom: 1px solid #eef2f6; }
    @media (max-width: 820px) {
      .grid, .ac-state { grid-template-columns: 1fr; }
      .buttons { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .status-bar, header { align-items: flex-start; flex-direction: column; }
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
    <a href="/settings">Settings</a>
  </header>

  <section class="status-bar">
    <div><span id="dot" class="dot"></span> <strong id="status">Starting</strong> <span id="detail" class="muted"></span></div>
    <div id="updated" class="muted">Last update —</div>
  </section>

  <section class="grid">
    <div class="card">
      <h2>Temperature</h2>
      <div id="temperature" class="metric temp">--.-°C</div>
      <div class="muted">Local sensor · DP 1</div>
    </div>
    <div class="card">
      <h2>Humidity</h2>
      <div id="humidity" class="metric humidity">--%</div>
      <div class="muted">Relative humidity · DP 2</div>
    </div>

    <div class="card">
      <h2>Air conditioner</h2>
      <div class="ac-state">
        <div id="powerState" class="power-state"><strong>—</strong><span>Assumed power</span></div>
        <div>
          <div id="chips" class="chips"></div>
          <p class="muted">Observed Sensibo/remote IR updates this assumed state. Sync changes state only.</p>
          <button class="sync" onclick="syncPower()">Sync Power</button>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>IR controls</h2>
      <div class="buttons" id="buttons"></div>
      <p id="lastResult" class="muted"></p>
    </div>

    <div class="card chart-card">
      <div class="chart-tools">
        <h2>History</h2>
        <select id="range" onchange="loadHistory()">
          <option value="1h" selected>Last hour</option>
          <option value="6h">Last 6 hours</option>
          <option value="24h">Last 24 hours</option>
          <option value="7d">Last 7 days</option>
          <option value="31d">Last 31 days</option>
        </select>
      </div>
      <canvas id="chart" width="1100" height="270"></canvas>
    </div>

    <div class="card events">
      <h2>Recent IR events</h2>
      <div id="events" class="muted">No events yet.</div>
    </div>
  </section>
</main>

<script>
const labels = {
  power: "Power", mode_heat: "Heat", mode_cool: "Cool", mode_dry: "Dry",
  mode_fan: "Fan", fan_low: "Low", fan_mid: "Mid", fan_high: "High",
  temp_down: "Temp −", temp_up: "Temp +", swing: "Swing", timer: "Timer", unit_cf: "C/F"
};
const order = ["power", "temp_down", "temp_up", "mode_heat", "mode_cool", "mode_dry", "mode_fan", "fan_low", "fan_mid", "fan_high", "swing", "timer", "unit_cf"];

function title(value) {
  if (!value) return "Unknown";
  return String(value).charAt(0).toUpperCase() + String(value).slice(1);
}

function sourceTitle(value) {
  return value === "localair" ? "LocalAIR" : title(value);
}

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
  powerBox.className = `power-state ${power}`;
  powerBox.innerHTML = `<strong>${title(power)}</strong><span>Assumed power</span>`;
  document.getElementById("chips").innerHTML = [
    ["Mode", data.ac_state.mode],
    ["Set", data.ac_state.temperature ? `${data.ac_state.temperature}°C` : null],
    ["Fan", data.ac_state.fan],
    ["Swing", data.ac_state.swing]
  ].map(([k, v]) => `<span class="chip">${k}: ${title(v)}</span>`).join("");

  const buttons = document.getElementById("buttons");
  buttons.innerHTML = order.map(command => {
    const disabled = data.ir_commands[command] ? "" : "disabled";
    const cls = command === "power" ? "primary" : "";
    return `<button class="${cls}" ${disabled} onclick="sendIR('${command}')">${labels[command]}</button>`;
  }).join("");

  document.getElementById("lastResult").textContent = data.last_result ? data.last_result.message : "";
  document.getElementById("events").innerHTML = data.events.length
    ? data.events.map(e => `<div class="event"><span>${sourceTitle(e.source)} · ${e.command || e.nec_hex || "Unknown"}</span><span>${new Date(e.timestamp).toLocaleTimeString()}</span></div>`).join("")
    : "No events yet.";
}

async function sendIR(command) {
  await fetch(`/api/ir/${command}`, {method: "POST"});
  setTimeout(refreshStatus, 500);
}

async function syncPower() {
  await fetch("/api/sync/power", {method: "POST"});
  refreshStatus();
}

async function loadHistory() {
  const range = document.getElementById("range").value;
  const data = await fetch(`/api/history?range=${range}`).then(r => r.json());
  drawChart(data.points);
}

function drawChart(points) {
  const canvas = document.getElementById("chart");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, w, h);
  const left = 44, right = 18, top = 18, bottom = 30;
  const gw = w - left - right, gh = h - top - bottom;
  ctx.strokeStyle = "#e4ebf2"; ctx.lineWidth = 1;
  for (let i = 0; i < 5; i++) {
    const y = top + gh * i / 4;
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + gw, y); ctx.stroke();
  }
  if (points.length === 0) {
    ctx.fillStyle = "#667085"; ctx.font = "15px sans-serif";
    ctx.fillText("History graph appears after the first stored reading", left + 245, top + gh / 2);
    return;
  }
  const temps = points.map(p => p.temperature_c);
  let minT = Math.floor(Math.min(...temps) - 1), maxT = Math.ceil(Math.max(...temps) + 1);
  if (maxT - minT < 4) { const mid = (maxT + minT) / 2; minT = mid - 2; maxT = mid + 2; }
  if (points.length === 1) {
    drawPoint(points[0].temperature_c, minT, maxT, "#ef6c42", left + gw / 2);
    drawPoint(points[0].humidity_percent, 0, 100, "#2170d1", left + gw / 2 + 12);
  } else {
    drawLine(points.map(p => p.temperature_c), minT, maxT, "#ef6c42", false);
    drawLine(points.map(p => p.humidity_percent), 0, 100, "#2170d1", true);
  }
  ctx.fillStyle = "#667085"; ctx.font = "13px sans-serif";
  ctx.fillText(`${maxT}°`, 5, top + 10); ctx.fillText(`${minT}°`, 5, top + gh);
  ctx.fillText(new Date(points[0].timestamp).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}), left, h - 8);

  function drawLine(values, min, max, color, dashed) {
    ctx.strokeStyle = color; ctx.lineWidth = 4; ctx.setLineDash(dashed ? [8, 6] : []);
    ctx.beginPath();
    values.forEach((value, i) => {
      const x = left + gw * i / (values.length - 1);
      const y = top + gh * (1 - (value - min) / (max - min));
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke(); ctx.setLineDash([]);
  }

  function drawPoint(value, min, max, color, x) {
    const y = top + gh * (1 - (value - min) / (max - min));
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fill();
  }
}

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
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #eef4f8; color: #172033; }}
    main {{ width: min(760px, calc(100% - 32px)); margin: 28px auto; }}
    .card {{ background: white; border: 1px solid #dde7ef; border-radius: 22px; padding: 22px; box-shadow: 0 18px 45px rgba(28,39,60,.08); }}
    h1 {{ margin-top: 0; }}
    label {{ display: block; margin: 14px 0 6px; font-weight: 800; }}
    input {{ width: 100%; padding: 11px; border: 1px solid #cfd8e3; border-radius: 12px; font: inherit; }}
    button, a.button {{ display: inline-block; margin-top: 18px; border: 0; border-radius: 13px; padding: 11px 16px; font-weight: 800; background: #2170d1; color: white; text-decoration: none; }}
    a {{ color: #2170d1; }}
    .muted {{ color: #667085; }}
  </style>
</head>
<body>
<main>
  <div class="card">
    <h1>LocalAIR Settings</h1>
    <p class="muted">The local key is saved to config only and is not printed by LocalAIR.</p>
    <form method="post">
      <label>IP address</label>
      <input name="ip_address" value="{ip_address}" required>
      <label>Device ID</label>
      <input name="device_id" value="{device_id}" required>
      <label>Local key</label>
      <input name="local_key" value="" placeholder="Leave blank to keep current key">
      <label>Protocol version</label>
      <input name="protocol_version" value="{protocol_version}" required>
      <label>Poll interval seconds</label>
      <input name="poll_interval_seconds" type="number" min="5" max="3600" value="{poll_interval_seconds}" required>
      <label>Web host</label>
      <input name="server_host" value="{server_host}" required>
      <label>Web port</label>
      <input name="server_port" type="number" min="1" max="65535" value="{server_port}" required>
      <label>History retention days</label>
      <input name="history_retention_days" type="number" min="1" max="365" value="{history_retention_days}" required>
      <button type="submit">Save settings</button>
      <a class="button" href="/" style="background:#eef4fb;color:#344054;">Cancel</a>
    </form>
  </div>
</main>
</body>
</html>
"""
