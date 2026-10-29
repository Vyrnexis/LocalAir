// LocalAIR dashboard: live status, room gauges, history charts, activity.

const NS = "http://www.w3.org/2000/svg";
const EVENTS_SHOWN = 8;
const RANGE_BUCKET_MINUTES = {"1h": 1, "24h": 5, "7d": 30, "30d": 120};
const HOUR = 3600e3;
const DAY = 24 * HOUR;

const state = {
  status: null,
  history: null,
  range: readSetting("range", "24h"),
  filter: "all",
  showAllEvents: false,
  busy: false,
};

function readSetting(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch (_error) { return fallback; }
}

function saveSetting(key, value) {
  try { localStorage.setItem(key, value); } catch (_error) {}
}

const $ = id => document.getElementById(id);
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Create one SVG element with attributes and optional text.
function el(parent, tag, attrs = {}, text) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  parent.appendChild(node);
  return node;
}

function clear(node) {
  while (node.firstChild) node.firstChild.remove();
}

// Format numbers and times in the viewer's locale.
const fmt1 = value => Number(value).toFixed(1);
const fmtDeg = value => `${Number(value) % 1 ? fmt1(value) : Number(value)} °C`;

function clockToDate(value, base = new Date()) {
  const [hour, minute] = value.split(":").map(Number);
  const date = new Date(base);
  date.setHours(hour, minute, 0, 0);
  return date;
}

function fmtClock(value) {
  return clockToDate(value).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
}

function fmtTime(date) {
  return date.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
}

function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function fmtWhen(date) {
  if (sameDay(date, new Date())) return fmtTime(date);
  return `${date.toLocaleDateString([], {day: "numeric", month: "short"})}, ${fmtTime(date)}`;
}

function fmtDuration(ms) {
  const minutes = Math.round(ms / 60000);
  const hours = Math.floor(minutes / 60);
  if (!hours) return `${minutes} min`;
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
}

// ---------------------------------------------------------------- status

async function refreshStatus() {
  try {
    const response = await fetch("/api/status");
    if (!response.ok) throw new Error(response.statusText);
    state.status = await response.json();
    renderStatus();
  } catch (_error) {
    setConnection(false, "LocalAIR is not responding", "");
  }
}

function setConnection(ok, text, time) {
  $("status").className = `status ${ok ? "ok" : "bad"}`;
  $("statusText").textContent = text;
  $("statusTime").textContent = time;
}

// The reading to show: the latest if valid, otherwise the last good one.
function roomReading() {
  const data = state.status;
  if (data.reading.ok) return data.reading;
  return data.last_valid;
}

function renderStatus() {
  const data = state.status;
  const reading = data.reading;
  const updated = new Date(reading.timestamp);
  setConnection(
    reading.ok,
    reading.ok ? "Connected" : reading.error_message || reading.status,
    reading.ok ? ` · updated ${fmtTime(updated)}` : "",
  );

  const room = roomReading();
  const temperature = room?.temperature_c;
  $("roomTemp").firstChild.textContent = temperature == null ? "--" : fmt1(temperature);
  $("roomHumidity").textContent = room?.humidity_percent == null ? "--%" : `${room.humidity_percent}%`;
  $("roomTemp").style.color = reading.ok ? "" : "var(--muted)";

  renderVerdict(temperature);
  renderAirConditioner();
  renderAutomation();
  drawBandGauge(temperature);
  renderEvents();
}

function renderVerdict(temperature) {
  const {climate, ac_state: ac} = state.status;
  const power = ac.power === "on" ? "on" : "off";
  let title = "Waiting for a sensor reading";
  if (temperature != null) {
    title = temperature > climate.turn_on_at
      ? "Above your comfort band"
      : temperature < climate.turn_off_at
        ? "Below your comfort band"
        : "Inside your comfort band";
  }
  let detail;
  if (!climate.enabled) detail = `The AC is ${power}. Automatic cooling is turned off.`;
  else if (climate.status === "Outside schedule") detail = `The AC is ${power}. Automatic cooling starts at ${fmtClock(climate.start_time)}.`;
  else if (climate.status === "Cooling") detail = `The AC is cooling. It turns off when the room reaches ${fmtDeg(climate.turn_off_at)}.`;
  else if (climate.status === "Sensor unavailable") detail = climate.detail;
  else detail = `The AC turns on if the room reaches ${fmtDeg(climate.turn_on_at)}.`;
  $("verdict").firstChild.textContent = title;
  $("verdictDetail").textContent = detail;
  $("bandLabel").textContent = `${fmt1(climate.turn_off_at)}–${fmt1(climate.turn_on_at)} °C`;
}

function renderAirConditioner() {
  const {ac_state: ac, ir_commands: commands, last_result: result} = state.status;
  const power = ac.power || "unknown";
  $("powerState").textContent = power === "unknown" ? "Unknown" : power === "on" ? "On" : "Off";
  $("powerState").className = `pill ${power === "on" ? "on" : ""}`;
  $("setpoint").textContent = ac.temperature ? `${ac.temperature}°` : "--°";
  const button = $("powerButton");
  button.textContent = power === "on" ? "Turn off" : "Turn on";
  button.className = `power-btn ${power === "on" ? "on" : ""}`;
  if (!state.busy) {
    button.disabled = !commands.power;
    $("tempDown").disabled = !commands.temp_down;
    $("tempUp").disabled = !commands.temp_up;
    const feedback = $("feedback");
    feedback.textContent = result ? result.message : commands.power ? "" : "Enter the device local key in Settings to control the AC.";
    feedback.className = `feedback ${result ? (result.ok ? "ok" : "bad") : ""}`;
  }
}

function renderAutomation() {
  const {climate, ac_state: ac} = state.status;
  const toggle = $("climateToggle");
  toggle.setAttribute("aria-pressed", String(climate.enabled));
  toggle.closest(".panel").classList.toggle("off", !climate.enabled);
  $("ruleSchedule").textContent = `${climate.start_time} – ${climate.end_time}`;
  $("ruleOn").textContent = fmtDeg(climate.turn_on_at);
  $("ruleOff").textContent = fmtDeg(climate.turn_off_at);

  const start = fmtClock(climate.start_time);
  const end = fmtClock(climate.end_time);
  const acTemp = `${climate.aircon_temperature} °C`;
  let title, detail, glyph = "";
  if (!climate.enabled) {
    title = "Automatic cooling is off";
    detail = `Turn it on to keep the room between ${fmtDeg(climate.turn_off_at)} and ${fmtDeg(climate.turn_on_at)} from ${start} to ${end}.`;
  } else if (climate.status === "Outside schedule") {
    title = `Waiting for the schedule to open at ${start}`;
    detail = `Until then the AC is yours to control. From ${start} it turns on at ${acTemp} whenever the room reaches ${fmtDeg(climate.turn_on_at)}.`;
  } else if (climate.status === "Cooling") {
    title = `Cooling until the room reaches ${fmtDeg(climate.turn_off_at)}`;
    detail = `The AC is set to ${ac.temperature || climate.aircon_temperature} °C. The schedule ends at ${end}, then the AC is turned off.`;
    glyph = "active";
  } else if (climate.status === "Sensor unavailable") {
    title = "No recent sensor reading";
    detail = `${climate.detail} The AC is kept off until readings return.`;
    glyph = "alert";
  } else {
    title = "Holding";
    detail = `The AC turns on at ${acTemp} if the room reaches ${fmtDeg(climate.turn_on_at)}. The schedule ends at ${end}.`;
    glyph = "active";
  }
  $("nextTitle").textContent = title;
  $("nextDetail").textContent = detail;
  $("nextGlyph").className = `glyph ${glyph}`;
}

// ---------------------------------------------------------------- gauges

function drawBandGauge(temperature) {
  const svg = $("bandGauge");
  clear(svg);
  if (!state.status) return;
  const {turn_on_at: on, turn_off_at: off} = state.status.climate;
  const values = [on, off, ...(temperature == null ? [] : [temperature])];
  let lo = Math.min(18, Math.floor(Math.min(...values) - 1));
  let hi = Math.max(30, Math.ceil(Math.max(...values) + 1));
  if (lo % 2) lo -= 1;
  if (hi % 2) hi += 1;
  const width = svg.clientWidth || 600;
  const pad = 6;
  const x = value => pad + (value - lo) / (hi - lo) * (width - pad * 2);
  const y = 22;
  const h = 14;

  const gradient = el(el(svg, "defs"), "linearGradient", {id: "warmth", x1: 0, x2: 1});
  el(gradient, "stop", {offset: 0, "stop-color": css("--warm"), "stop-opacity": .08});
  el(gradient, "stop", {offset: 1, "stop-color": css("--warm"), "stop-opacity": .6});
  el(svg, "rect", {x: x(lo), y, width: x(hi) - x(lo), height: h, rx: 7, fill: css("--surface-2")});
  el(svg, "rect", {x: x(on), y, width: x(hi) - x(on), height: h, rx: 7, fill: "url(#warmth)"});
  el(svg, "rect", {x: x(off), y: y - 4, width: Math.max(2, x(on) - x(off)), height: h + 8, rx: 6, fill: css("--cool-band"), stroke: css("--cool"), "stroke-width": 1.5});
  el(svg, "text", {x: x(off) - 2, y: y - 10, "text-anchor": "end", fill: css("--muted"), "font-size": 12}, `off at ${fmt1(off)}°`);
  el(svg, "text", {x: x(on) + 2, y: y - 10, "text-anchor": "start", fill: css("--muted"), "font-size": 12}, `on at ${fmt1(on)}°`);
  const step = width < 420 ? 4 : 2;
  for (let value = lo; value <= hi; value += step) {
    el(svg, "text", {
      x: x(value), y: y + h + 24, fill: css("--faint"), "font-size": 13,
      "text-anchor": value === lo ? "start" : value + step > hi ? "end" : "middle",
    }, `${value}°`);
  }
  if (temperature == null) return;
  const now = x(temperature);
  el(svg, "line", {x1: now, x2: now, y1: y - 8, y2: y + h + 8, stroke: css("--ink"), "stroke-width": 3, "stroke-linecap": "round"});
  el(svg, "circle", {cx: now, cy: y + h / 2, r: 7, fill: css("--warm"), stroke: css("--bg"), "stroke-width": 3});
  svg.setAttribute("aria-label", `Room ${fmt1(temperature)} °C, comfort band ${fmt1(off)} to ${fmt1(on)} °C`);
}

function drawTodayStrip() {
  const svg = $("todayStrip");
  clear(svg);
  const data = state.history;
  const climate = state.status?.climate;
  if (!data || !climate) return;
  const start = Date.parse(data.today.start);
  const end = start + DAY;
  const now = Date.now();
  const width = svg.clientWidth || 600;
  const pad = 6;
  const x = t => pad + Math.min(1, Math.max(0, (t - start) / DAY)) * (width - pad * 2);
  const y = 18;
  const h = 22;

  el(svg, "rect", {x: x(start), y, width: x(end) - x(start), height: h, rx: 6, fill: css("--surface")});
  const day = new Date(start);
  const open = clockToDate(climate.start_time, day).getTime();
  const close = clockToDate(climate.end_time, day).getTime();
  el(svg, "rect", {
    x: x(open), y: y - 5, width: x(close) - x(open), height: h + 10, rx: 7, fill: "none",
    stroke: climate.enabled ? css("--muted") : css("--faint"), "stroke-dasharray": "3 3",
  });
  let ran = 0;
  for (const run of data.today.runs) {
    const a = Math.max(start, Date.parse(run.start));
    const b = Math.min(now, Date.parse(run.end));
    if (b <= a) continue;
    ran += b - a;
    el(svg, "rect", {x: x(a), y: y + 3, width: Math.max(3, x(b) - x(a)), height: h - 6, rx: 4, fill: css("--cool")});
  }
  el(svg, "rect", {x: x(now), y, width: Math.max(0, x(end) - x(now)), height: h, fill: css("--bg"), opacity: .5});
  el(svg, "line", {x1: x(now), x2: x(now), y1: y - 8, y2: y + h + 8, stroke: css("--ink"), "stroke-width": 2});
  const nearEnd = x(now) > width - 40;
  el(svg, "text", {
    x: x(now) + (nearEnd ? -6 : 6), y: 10, "text-anchor": nearEnd ? "end" : "start",
    fill: css("--ink"), "font-size": 12, "font-weight": 600,
  }, "now");
  const every = width < 500 ? 6 : 3;
  for (let hour = 0; hour <= 24; hour += every) {
    const t = new Date(start);
    t.setHours(hour, 0, 0, 0);
    const label = hour === 24 ? "midnight" : hour === 0 ? "12 am" : t.toLocaleTimeString([], {hour: "numeric"});
    el(svg, "text", {
      x: x(hour === 24 ? end : t.getTime()), y: y + h + 26, fill: css("--faint"), "font-size": 13,
      "text-anchor": hour === 0 ? "start" : hour === 24 ? "end" : "middle",
    }, label);
  }
  $("todaySummary").textContent = ran ? `AC ran ${fmtDuration(ran)}` : "AC hasn't run today";
}

// ---------------------------------------------------------------- history

async function loadHistory() {
  try {
    const response = await fetch(`/api/history?range=${encodeURIComponent(state.range)}`);
    if (!response.ok) return;
    state.history = await response.json();
    drawHistory();
  } catch (_error) {}
}

function setRange(range) {
  state.range = range;
  saveSetting("range", range);
  document.querySelectorAll(".seg button").forEach(button =>
    button.setAttribute("aria-checked", String(button.dataset.range === range))
  );
  loadHistory();
}

// Round a domain outward to whole steps so gridlines land on labels.
function niceDomain(values, minSpan, step) {
  let lo = Math.floor(Math.min(...values) / step) * step;
  let hi = Math.ceil(Math.max(...values) / step) * step;
  while (hi - lo < minSpan) { lo -= step; if (hi - lo < minSpan) hi += step; }
  return [lo, hi];
}

// Local-time tick positions for the selected range.
function timeTicks(start, end, width) {
  const narrow = width < 600;
  const ticks = [];
  const range = state.range;
  if (range === "1h") {
    const step = (narrow ? 20 : 10) * 60e3;
    for (let t = Math.ceil(start / step) * step; t < end; t += step) {
      ticks.push([t, fmtTime(new Date(t))]);
    }
    return ticks;
  }
  if (range === "24h") {
    const hours = narrow ? 6 : 3;
    const t = new Date(start);
    t.setMinutes(0, 0, 0);
    while (t.getTime() <= start || t.getHours() % hours) t.setHours(t.getHours() + 1);
    for (; t.getTime() < end; t.setHours(t.getHours() + hours)) {
      ticks.push([t.getTime(), t.toLocaleTimeString([], {hour: "numeric"})]);
    }
    return ticks;
  }
  const days = range === "7d" ? (narrow ? 2 : 1) : (narrow ? 10 : 5);
  const t = new Date(start);
  t.setHours(24, 0, 0, 0);
  for (let i = 0; t.getTime() < end; t.setDate(t.getDate() + 1), i++) {
    if (i % days) continue;
    const label = range === "7d"
      ? t.toLocaleDateString([], {weekday: "short"})
      : t.toLocaleDateString([], {day: "numeric", month: "short"});
    ticks.push([t.getTime(), label]);
  }
  return ticks;
}

// Shared geometry for both history charts.
function chartFrame(svg, height, showAxis) {
  const width = svg.clientWidth || 800;
  const left = 34;
  const right = 10;
  const top = 10;
  const bottom = showAxis ? 28 : 8;
  const {start, end} = historyWindow();
  return {
    width, height, left, right, top, bottom, start, end,
    x: t => left + (t - start) / (end - start) * (width - left - right),
    plotBottom: height - bottom,
  };
}

function historyWindow() {
  return {start: Date.parse(state.history.start), end: Date.parse(state.history.end)};
}

function historyPoints() {
  return state.history.points.map(point => ({
    t: Date.parse(point.timestamp),
    temp: point.temperature_c,
    hum: point.humidity_percent,
  }));
}

function acRuns() {
  return state.history.ac_runs.map(run => [Date.parse(run.start), Date.parse(run.end)]);
}

function drawChart(svg, options) {
  clear(svg);
  const {height, key, color, domain, step, band, showAxis, points} = options;
  const f = chartFrame(svg, height, showAxis);
  const [lo, hi] = domain;
  const y = value => f.top + (1 - (value - lo) / (hi - lo)) * (f.plotBottom - f.top);
  const plotHeight = f.plotBottom - f.top;

  // Schedule windows, then AC running periods behind everything else.
  const climate = state.status?.climate;
  if (climate && state.range !== "30d") {
    for (let day = new Date(f.start); day.getTime() < f.end + DAY; day.setDate(day.getDate() + 1)) {
      const open = Math.max(f.start, clockToDate(climate.start_time, day).getTime());
      const close = Math.min(f.end, clockToDate(climate.end_time, day).getTime());
      if (close > open) el(svg, "rect", {x: f.x(open), y: f.top, width: f.x(close) - f.x(open), height: plotHeight, fill: css("--schedule")});
    }
  }
  for (const [a, b] of acRuns()) {
    const from = Math.max(a, f.start);
    const to = Math.min(b, f.end);
    if (to > from) el(svg, "rect", {x: f.x(from), y: f.top, width: Math.max(1, f.x(to) - f.x(from)), height: plotHeight, fill: css("--cool-tint")});
  }
  if (band) {
    el(svg, "rect", {x: f.left, y: y(band[1]), width: f.width - f.left - f.right, height: y(band[0]) - y(band[1]), fill: css("--band")});
    for (const value of band) {
      el(svg, "line", {x1: f.left, x2: f.width - f.right, y1: y(value), y2: y(value), stroke: css("--faint"), "stroke-dasharray": "4 4"});
    }
  }
  for (let value = lo; value <= hi + 1e-9; value += step) {
    el(svg, "line", {x1: f.left, x2: f.width - f.right, y1: y(value), y2: y(value), stroke: css("--grid")});
    el(svg, "text", {x: f.left - 8, y: y(value) + 4, "text-anchor": "end", fill: css("--faint"), "font-size": 12}, String(value));
  }
  if (showAxis) {
    const nowX = f.x(f.end);
    for (const [t, label] of timeTicks(f.start, f.end, f.width)) {
      if (f.x(t) > nowX - 52 || f.x(t) < f.left + 16) continue;
      el(svg, "text", {x: f.x(t), y: height - 6, "text-anchor": "middle", fill: css("--faint"), "font-size": 12}, label);
    }
    el(svg, "text", {x: nowX, y: height - 6, "text-anchor": "end", fill: css("--muted"), "font-size": 12, "font-weight": 600}, "now");
  }

  // Break the line across gaps in the readings.
  const gap = (RANGE_BUCKET_MINUTES[state.range] || 5) * 60e3 * 3;
  let path = "";
  let previous = null;
  for (const point of points) {
    const command = previous && point.t - previous.t <= gap ? "L" : "M";
    path += `${command}${f.x(point.t).toFixed(1)},${y(point[key]).toFixed(1)}`;
    previous = point;
  }
  el(svg, "path", {d: path, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"});
  el(svg, "g", {class: "hover"});
  svg._frame = {...f, y, key, color};
}

function drawHistory() {
  if (!state.history || !state.status) return;
  drawTodayStrip();
  const points = historyPoints();
  const empty = points.length === 0;
  $("chartEmpty").hidden = !empty;
  const climate = state.status.climate;
  const temps = empty ? [climate.turn_off_at, climate.turn_on_at] : points.map(p => p.temp);
  const hums = empty ? [40, 70] : points.map(p => p.hum);
  const tempSpan = Math.max(...temps, climate.turn_on_at) - Math.min(...temps, climate.turn_off_at);
  const tempStep = tempSpan > 10 ? 2 : 1;
  drawChart($("tempChart"), {
    height: 230, key: "temp", color: css("--warm"), points, step: tempStep,
    domain: niceDomain([...temps, climate.turn_off_at - .5, climate.turn_on_at + .5], 4, tempStep),
    band: [climate.turn_off_at, climate.turn_on_at], showAxis: false,
  });
  drawChart($("humidChart"), {
    height: 150, key: "hum", color: css("--humid"), points, step: 10,
    domain: niceDomain(hums, 20, 10), band: null, showAxis: true,
  });
  hideHover();
}

// ---------------------------------------------------------------- hover

function nearestPoint(points, t) {
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].t < t) lo = mid + 1; else hi = mid;
  }
  if (lo > 0 && t - points[lo - 1].t < points[lo].t - t) lo -= 1;
  return points[lo];
}

function showHover(event) {
  if (!state.history) return;
  const points = historyPoints();
  if (!points.length) return;
  const svg = event.currentTarget;
  const frame = svg._frame;
  const rect = svg.getBoundingClientRect();
  const px = event.clientX - rect.left;
  if (px < frame.left || px > frame.width - frame.right) return hideHover();
  const t = frame.start + (px - frame.left) / (frame.width - frame.left - frame.right) * (frame.end - frame.start);
  const point = nearestPoint(points, t);

  for (const chart of [$("tempChart"), $("humidChart")]) {
    const f = chart._frame;
    const group = chart.querySelector(".hover");
    clear(group);
    const x = f.x(point.t);
    el(group, "line", {x1: x, x2: x, y1: f.top, y2: f.plotBottom, stroke: css("--muted"), "stroke-width": 1});
    el(group, "circle", {cx: x, cy: f.y(point[f.key]), r: 5, fill: f.color, stroke: css("--surface"), "stroke-width": 2});
  }

  const running = acRuns().some(([a, b]) => point.t >= a && point.t <= b);
  const when = new Date(point.t);
  const tooltip = $("tooltip");
  tooltip.innerHTML = "";
  const line = (color, label, value) => {
    const row = document.createElement("div");
    row.innerHTML = `<span><i style="background:${color}"></i></span><strong></strong>`;
    row.firstChild.append(label);
    row.lastChild.textContent = value;
    tooltip.append(row);
  };
  const title = document.createElement("div");
  title.className = "t";
  title.textContent = state.range === "1h" || state.range === "24h"
    ? fmtWhen(when)
    : `${when.toLocaleDateString([], {weekday: "short", day: "numeric", month: "short"})}, ${fmtTime(when)}`;
  tooltip.append(title);
  line("var(--warm)", "Temperature", `${fmt1(point.temp)} °C`);
  line("var(--humid)", "Humidity", `${point.hum}%`);
  line(running ? "var(--cool)" : "var(--faint)", "AC", running ? "Running" : "Off");
  tooltip.hidden = false;

  const wrap = $("charts").getBoundingClientRect();
  const x = rect.left - wrap.left + frame.x(point.t);
  const width = tooltip.offsetWidth;
  const left = x + 16 + width > wrap.width - 8 ? x - 16 - width : x + 16;
  tooltip.style.left = `${Math.max(8, left)}px`;
  // SVG elements have no offsetTop, so position from the bounding boxes.
  tooltip.style.top = `${$("tempChart").getBoundingClientRect().top - wrap.top + 20}px`;
}

function hideHover() {
  $("tooltip").hidden = true;
  for (const chart of [$("tempChart"), $("humidChart")]) {
    const group = chart.querySelector(".hover");
    if (group) clear(group);
  }
}

// ---------------------------------------------------------------- activity

const ICONS = {
  cool: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 2v20M4 7l16 10M20 7L4 17"/></svg>',
  off: '<svg viewBox="0 0 24 24" width="12" height="12"><rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor"/></svg>',
  set: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M14 14.8V4a2 2 0 1 0-4 0v10.8a4 4 0 1 0 4 0z"/></svg>',
  bad: "!",
};

function describeEvent(event) {
  if (event.kind === "device") {
    return {icon: "bad", text: event.error_message || `Sensor ${String(event.status).toLowerCase()}`, who: "Device"};
  }
  const who = {climate: "Automatic", localair: "Manual", observed: "Remote"}[event.source] || "Manual";
  const command = event.command || "";
  if (command.startsWith("temperature_")) return {icon: "set", text: `AC set to ${command.split("_")[1]} °C`, who};
  const known = {
    climate_on: ["cool", "Cooling started"],
    climate_off: ["off", "Cooling stopped"],
    schedule_end: ["off", "Schedule ended, AC turned off"],
    power_on: ["cool", "AC turned on"],
    power_off: ["off", "AC turned off"],
  }[command];
  if (known) return {icon: known[0], text: known[1], who};
  if (!command) return {icon: "off", text: "Unrecognised remote signal", who};
  return {icon: "off", text: command.replaceAll("_", " "), who};
}

function eventCategory(event) {
  if (event.kind === "device") return "errors";
  return event.source === "climate" ? "automatic" : "manual";
}

function renderEvents() {
  const list = $("events");
  const matching = (state.status?.events || []).filter(event =>
    state.filter === "all" || eventCategory(event) === state.filter
  );
  const events = state.showAllEvents ? matching : matching.slice(0, EVENTS_SHOWN);
  const more = $("eventsMore");
  more.hidden = matching.length <= EVENTS_SHOWN;
  more.textContent = state.showAllEvents ? "Show fewer" : `Show ${matching.length - EVENTS_SHOWN} more`;
  list.replaceChildren(...events.map(event => {
    const info = describeEvent(event);
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.dateTime = event.timestamp;
    time.textContent = fmtWhen(new Date(event.timestamp));
    const icon = document.createElement("span");
    icon.className = `ic ${info.icon === "set" ? "cool" : info.icon}`;
    icon.innerHTML = ICONS[info.icon];
    const text = document.createElement("span");
    text.textContent = info.text;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = info.who;
    item.append(time, icon, text, who);
    return item;
  }));
  $("eventsEmpty").hidden = matching.length > 0;
}

// ---------------------------------------------------------------- actions

async function post(url) {
  const response = await fetch(url, {method: "POST"});
  let body = {};
  try { body = await response.json(); } catch (_error) {}
  return {ok: response.ok, body};
}

async function sendIR(command) {
  const feedback = $("feedback");
  state.busy = true;
  for (const id of ["powerButton", "tempDown", "tempUp"]) $(id).disabled = true;
  feedback.className = "feedback";
  feedback.textContent = command === "power" ? "Sending…" : "Changing temperature…";
  try {
    const {ok, body} = await post(`/api/ir/${command}`);
    feedback.className = `feedback ${ok ? "ok" : "bad"}`;
    feedback.textContent = body.message || body.detail || "The command failed.";
  } catch (_error) {
    feedback.className = "feedback bad";
    feedback.textContent = "LocalAIR could not be reached.";
  }
  state.busy = false;
  await refreshStatus();
  loadHistory();
}

async function syncPower(power) {
  await post(`/api/sync/power/${power}`);
  await refreshStatus();
  loadHistory();
}

async function toggleClimate() {
  const toggle = $("climateToggle");
  const enabled = state.status?.climate.enabled;
  toggle.disabled = true;
  await post(`/api/climate/${enabled ? "off" : "on"}`);
  await refreshStatus();
  toggle.disabled = false;
}

// ---------------------------------------------------------------- wiring

$("powerButton").addEventListener("click", () => sendIR("power"));
$("tempDown").addEventListener("click", () => sendIR("temp_down"));
$("tempUp").addEventListener("click", () => sendIR("temp_up"));
$("climateToggle").addEventListener("click", toggleClimate);
document.querySelectorAll("[data-power]").forEach(button =>
  button.addEventListener("click", () => syncPower(button.dataset.power))
);
document.querySelectorAll(".seg button").forEach(button =>
  button.addEventListener("click", () => setRange(button.dataset.range))
);
document.querySelectorAll(".chips button").forEach(button =>
  button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll(".chips button").forEach(other => other.classList.toggle("on", other === button));
    renderEvents();
  })
);
$("eventsMore").addEventListener("click", () => {
  state.showAllEvents = !state.showAllEvents;
  renderEvents();
});
for (const chart of [$("tempChart"), $("humidChart")]) {
  chart.addEventListener("pointermove", showHover);
  chart.addEventListener("pointerleave", hideHover);
}

let redrawQueued = false;
function redraw() {
  if (redrawQueued) return;
  redrawQueued = true;
  requestAnimationFrame(() => {
    redrawQueued = false;
    if (state.status) drawBandGauge(roomReading()?.temperature_c);
    drawHistory();
  });
}
new ResizeObserver(redraw).observe($("charts"));
document.addEventListener("themechange", redraw);

setRange(state.range in RANGE_BUCKET_MINUTES ? state.range : "24h");
refreshStatus().then(loadHistory);
setInterval(refreshStatus, 3000);
setInterval(loadHistory, 30000);
