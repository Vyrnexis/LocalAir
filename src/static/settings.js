// LocalAIR settings: load, edit, validate, save, and test the sensor.

const NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const form = $("form");
const NUMBER_FIELDS = new Set([
  "sensor_timeout_seconds", "protocol_version", "poll_interval_seconds",
  "history_retention_days", "server_port",
]);
const PERIOD_FIELDS = ["name", "enabled", "start", "end", "turn_on_at", "turn_off_at", "aircon_temperature"];
const PERIOD_NUMBERS = new Set(["turn_on_at", "turn_off_at", "aircon_temperature"]);
const MAX_PERIODS = 4;
const DAY_MINUTES = 24 * 60;
let saved = null;

// One period card. Values are set through the inputs, never as markup.
const PERIOD_TEMPLATE = `
  <div class="period-head">
    <label class="f name"><span class="sr-only">Name</span><span class="input"><input data-period="name" maxlength="24" autocomplete="off"></span></label>
    <label class="period-on"><span>On</span><input class="switch" type="checkbox" role="switch" data-period="enabled"></label>
    <button class="btn-quiet remove" type="button">Remove</button>
  </div>
  <div class="period-fields">
    <label class="f"><span>Start</span><span class="input"><input type="time" data-period="start" required></span></label>
    <label class="f"><span>End</span><span class="input"><input type="time" data-period="end" required></span></label>
    <label class="f"><span>Turn the AC on when the room reaches</span><span class="input"><input type="number" data-period="turn_on_at" step="0.1" min="-20" max="60" inputmode="decimal" required><em>°C</em></span></label>
    <label class="f"><span>Turn the AC off when the room drops to</span><span class="input"><input type="number" data-period="turn_off_at" step="0.1" min="-20" max="60" inputmode="decimal" required><em>°C</em></span></label>
    <label class="f"><span>AC temperature when cooling</span><span class="input"><input type="number" data-period="aircon_temperature" min="16" max="30" step="1" inputmode="numeric" required><em>°C</em></span><small>What the AC is set to when this period turns it on (16–30 °C).</small></label>
    <p class="span-note"></p>
  </div>
  <div class="preview">
    <p class="band-text"></p>
    <svg class="band-preview" height="64" aria-hidden="true"></svg>
  </div>`;

function el(parent, tag, attrs = {}, text) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  parent.appendChild(node);
  return node;
}

const inputs = () => [...form.elements].filter(input => input.name);
const cards = () => [...$("periods").children];
const toMinutes = value => {
  const [hour, minute] = value.split(":").map(Number);
  return hour * 60 + minute;
};
const fmtClock = value => {
  const date = new Date();
  date.setHours(...value.split(":").map(Number), 0, 0);
  return date.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
};
const periodLabel = (period, index) => period.name.trim() || `Period ${index + 1}`;

// ---------------------------------------------------------------- periods

function readPeriod(card) {
  const period = {};
  for (const input of card.querySelectorAll("[data-period]")) {
    const key = input.dataset.period;
    if (input.type === "checkbox") period[key] = input.checked;
    else if (PERIOD_NUMBERS.has(key)) period[key] = input.value === "" ? "" : Number(input.value);
    else period[key] = input.value.trim();
  }
  return period;
}

const readPeriods = () => cards().map(readPeriod);

function addCard(period) {
  const card = document.createElement("div");
  card.className = "period";
  card.innerHTML = PERIOD_TEMPLATE;
  for (const input of card.querySelectorAll("[data-period]")) {
    const value = period[input.dataset.period];
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
  }
  card.querySelector(".remove").addEventListener("click", () => {
    card.remove();
    update();
  });
  $("periods").append(card);
}

const toClock = minutes => `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;

// The longest stretch of the day no switched-on period covers, as
// [start, end] clock times, or null when periods cover the whole day.
function largestGap(periods) {
  const covered = new Array(DAY_MINUTES).fill(false);
  for (const period of periods) {
    if (!period.enabled || !complete(period)) continue;
    for (const [a, b] of ranges(period)) covered.fill(true, a, b);
  }
  const firstCovered = covered.indexOf(true);
  if (firstCovered === -1) return ["16:00", "09:00"];
  let best = null;
  let runStart = null;
  // Walk one full day starting at a covered minute, so a gap that crosses
  // midnight is found as a single run.
  for (let step = 1; step <= DAY_MINUTES; step++) {
    const minute = (firstCovered + step) % DAY_MINUTES;
    if (!covered[minute] && runStart === null) runStart = step;
    if (covered[minute] && runStart !== null) {
      if (!best || step - runStart > best[1] - best[0]) best = [runStart, step];
      runStart = null;
    }
  }
  if (!best) return null;
  return best.map(step => toClock((firstCovered + step) % DAY_MINUTES));
}

// A new period fills the largest free stretch of the day, with the last
// period's temperatures.
function addPeriod() {
  const periods = readPeriods();
  const gap = largestGap(periods);
  if (!gap) return;
  const last = periods[periods.length - 1];
  addCard({
    name: "", enabled: true, start: gap[0], end: gap[1],
    turn_on_at: last?.turn_on_at ?? 24, turn_off_at: last?.turn_off_at ?? 22,
    aircon_temperature: last?.aircon_temperature ?? 16,
  });
  update();
  cards().at(-1).querySelector('[data-period="name"]').focus();
}

// Minute ranges of a period within one day, split at midnight.
function ranges(period) {
  const start = toMinutes(period.start);
  const end = toMinutes(period.end);
  return start < end ? [[start, end]] : [[start, DAY_MINUTES], [0, end]];
}

const complete = period => /^\d\d:\d\d$/.test(period.start) && /^\d\d:\d\d$/.test(period.end) && period.start !== period.end;

// The first pair of switched-on periods that share any minute.
function findOverlap(periods) {
  for (let i = 0; i < periods.length; i++) {
    for (let j = 0; j < i; j++) {
      const a = periods[i], b = periods[j];
      if (!a.enabled || !b.enabled || !complete(a) || !complete(b)) continue;
      if (ranges(a).some(([s1, e1]) => ranges(b).some(([s2, e2]) => s1 < e2 && s2 < e1))) return [j, i];
    }
  }
  return null;
}

function refreshPeriods() {
  const periods = readPeriods();
  cards().forEach((card, index) => {
    const period = periods[index];
    card.querySelector('[data-period="name"]').placeholder = `Period ${index + 1}`;
    card.querySelector(".remove").hidden = periods.length === 1;
    card.classList.toggle("disabled", !period.enabled);
    const note = card.querySelector(".span-note");
    note.textContent = !complete(period)
      ? "The start and end times must differ."
      : `Runs daily ${fmtClock(period.start)} to ${fmtClock(period.end)}${period.start > period.end ? ", overnight" : ""}.`;
    note.classList.toggle("bad", !complete(period));
    drawBandPreview(card, period, index);
  });
  const full = periods.length >= MAX_PERIODS;
  const noGap = !full && !largestGap(periods);
  $("addPeriod").disabled = full || noGap;
  $("periodHint").textContent = full
    ? `Up to ${MAX_PERIODS} periods.`
    : noGap
      ? "The periods cover the whole day. Shorten one to make room for another."
      : "Periods can meet end to end but can't overlap.";
  drawTimeline(periods);
}

// A 24-hour bar showing when each period runs, and any overlap.
function drawTimeline(periods) {
  const svg = $("timeline");
  while (svg.firstChild) svg.firstChild.remove();
  const overlap = findOverlap(periods);
  const text = $("timelineText");
  if (overlap) {
    text.className = "bad";
    text.textContent = `${periodLabel(periods[overlap[1]], overlap[1])} overlaps ${periodLabel(periods[overlap[0]], overlap[0])}.`;
  } else {
    const on = periods.filter(period => period.enabled && complete(period));
    const minutes = on.reduce((sum, period) => sum + ranges(period).reduce((s, [a, b]) => s + b - a, 0), 0);
    const hours = Math.round(minutes / 6) / 10;
    text.className = "";
    text.textContent = on.length
      ? `Automatic cooling covers ${hours} of 24 hours. The rest of the day is manual.`
      : "No periods are switched on.";
    if (minutes === DAY_MINUTES) text.textContent = "Automatic cooling covers the whole day.";
  }

  const width = svg.clientWidth || 600;
  const pad = 2;
  const x = minute => pad + minute / DAY_MINUTES * (width - pad * 2);
  const y = 6;
  const h = 26;
  el(svg, "rect", {x: x(0), y, width: x(DAY_MINUTES) - x(0), height: h, rx: 6, fill: css("--surface-2")});
  periods.forEach((period, index) => {
    if (!complete(period)) return;
    const clash = overlap?.includes(index);
    for (const [a, b] of ranges(period)) {
      el(svg, "rect", {
        x: x(a), y, width: Math.max(2, x(b) - x(a)), height: h, rx: 6,
        fill: !period.enabled ? "none" : clash ? css("--bad-tint") : css("--cool-band"),
        stroke: !period.enabled ? css("--faint") : clash ? css("--bad") : css("--cool"),
        "stroke-width": 1.5, "stroke-dasharray": period.enabled ? "" : "3 3",
      });
      if (x(b) - x(a) > 70) {
        el(svg, "text", {
          x: (x(a) + x(b)) / 2, y: y + h / 2 + 4, "text-anchor": "middle",
          fill: period.enabled ? css("--ink") : css("--muted"), "font-size": 12, "font-weight": 600,
        }, periodLabel(period, index));
      }
    }
  });
  const every = width < 420 ? 6 : 3;
  for (let hour = 0; hour <= 24; hour += every) {
    const date = new Date();
    date.setHours(hour % 24, 0, 0, 0);
    el(svg, "text", {
      x: x(hour * 60), y: y + h + 18, fill: css("--faint"), "font-size": 12,
      "text-anchor": hour === 0 ? "start" : hour === 24 ? "end" : "middle",
    }, hour === 24 ? "midnight" : date.toLocaleTimeString([], {hour: "numeric"}));
  }
}

function drawBandPreview(card, period, index) {
  const svg = card.querySelector(".band-preview");
  while (svg.firstChild) svg.firstChild.remove();
  const on = period.turn_on_at;
  const off = period.turn_off_at;
  const text = card.querySelector(".band-text");
  const valid = on !== "" && off !== "" && off < on;
  svg.style.display = valid ? "" : "none";
  if (!valid) {
    text.className = "band-text bad";
    text.textContent = "The turn-off temperature must be lower than the turn-on temperature.";
    return;
  }
  text.className = "band-text";
  text.innerHTML = "The room will be kept between <strong></strong> and <strong></strong>.";
  text.children[0].textContent = `${off.toFixed(1)} °C`;
  text.children[1].textContent = `${on.toFixed(1)} °C`;

  let lo = Math.min(18, Math.floor(off - 2));
  let hi = Math.max(30, Math.ceil(on + 2));
  if (lo % 2) lo -= 1;
  if (hi % 2) hi += 1;
  const width = svg.clientWidth || 600;
  const pad = 4;
  const x = value => pad + (value - lo) / (hi - lo) * (width - pad * 2);
  const y = 20;
  const h = 12;
  const id = `warmth-${index}`;
  const gradient = el(el(svg, "defs"), "linearGradient", {id, x1: 0, x2: 1});
  el(gradient, "stop", {offset: 0, "stop-color": css("--warm"), "stop-opacity": .08});
  el(gradient, "stop", {offset: 1, "stop-color": css("--warm"), "stop-opacity": .6});
  el(svg, "rect", {x: x(lo), y, width: x(hi) - x(lo), height: h, rx: 6, fill: css("--surface-2")});
  el(svg, "rect", {x: x(on), y, width: x(hi) - x(on), height: h, rx: 6, fill: `url(#${id})`});
  el(svg, "rect", {x: x(off), y: y - 4, width: Math.max(2, x(on) - x(off)), height: h + 8, rx: 6, fill: css("--cool-band"), stroke: css("--cool"), "stroke-width": 1.5});
  el(svg, "text", {x: x(off) - 2, y: y - 9, "text-anchor": "end", fill: css("--muted"), "font-size": 12}, `off at ${off.toFixed(1)}°`);
  el(svg, "text", {x: x(on) + 2, y: y - 9, "text-anchor": "start", fill: css("--muted"), "font-size": 12}, `on at ${on.toFixed(1)}°`);
  const step = width < 420 ? 4 : 2;
  for (let value = lo; value <= hi; value += step) {
    el(svg, "text", {
      x: x(value), y: y + h + 20, fill: css("--faint"), "font-size": 12,
      "text-anchor": value === lo ? "start" : value + step > hi ? "end" : "middle",
    }, `${value}°`);
  }
}

// ---------------------------------------------------------------- form

// Current form values in the shape the API expects.
function formValues() {
  const values = {};
  for (const input of inputs()) {
    if (input.disabled) continue;
    if (input.type === "checkbox") values[input.name] = input.checked;
    else if (input.name === "local_key") values.local_key = input.value.trim();
    else if (NUMBER_FIELDS.has(input.name)) values[input.name] = input.value === "" ? "" : Number(input.value);
    else values[input.name] = input.value.trim();
  }
  values.climate_periods = readPeriods();
  return values;
}

function fill(settings) {
  saved = settings;
  for (const input of inputs()) {
    if (input.type === "checkbox") input.checked = Boolean(settings[input.name]);
    else if (input.name === "local_key") input.value = "";
    else input.value = settings[input.name] ?? "";
  }
  $("periods").replaceChildren();
  for (const period of settings.climate_periods) addCard(period);
  $("keySaved").hidden = !settings.local_key_saved;
  // Show the values the container actually uses, read-only.
  const env = settings.env_overrides || {};
  for (const name of ["server_host", "server_port"]) {
    const input = form.elements[name];
    input.disabled = Boolean(env[name]);
    if (env[name]) input.value = env[name];
  }
  $("envNote").hidden = !(env.server_host || env.server_port);
  $("restartNote").hidden = Boolean(env.server_host && env.server_port);
  clearErrors();
  update();
}

// Number of edited fields, counting each period field and each added or
// removed period.
function changeCount() {
  if (!saved) return 0;
  const values = formValues();
  let count = Object.keys(values).filter(name =>
    name === "climate_periods" ? false
      : name === "local_key" ? values.local_key !== ""
        : values[name] !== saved[name]
  ).length;
  const now = values.climate_periods;
  const before = saved.climate_periods;
  count += Math.abs(now.length - before.length);
  for (let i = 0; i < Math.min(now.length, before.length); i++) {
    count += PERIOD_FIELDS.filter(key => now[i][key] !== before[i][key]).length;
  }
  return count;
}

// Refresh the save bar and previews after any edit.
function update() {
  const changed = changeCount();
  const status = $("saveStatus");
  if (!status.classList.contains("bad") || changed === 0) {
    status.className = "";
    status.innerHTML = changed
      ? `<b>${changed} unsaved ${changed === 1 ? "change" : "changes"}</b>`
      : "No unsaved changes";
  }
  $("save").disabled = changed === 0;
  $("discard").disabled = changed === 0;
  refreshPeriods();
}

function clearErrors() {
  form.querySelectorAll(".invalid").forEach(label => label.classList.remove("invalid"));
  form.querySelectorAll(".error").forEach(error => error.remove());
}

// Find the input for an API field name, including "climate_periods.1.end".
function fieldInput(field) {
  const match = /^climate_periods\.(\d+)\.(\w+)$/.exec(field || "");
  if (match) return cards()[Number(match[1])]?.querySelector(`[data-period="${match[2]}"]`);
  return field ? form.elements[field] : null;
}

function showError(field, message) {
  const status = $("saveStatus");
  status.className = "bad";
  status.textContent = message;
  const input = fieldInput(field);
  if (!input) return;
  const label = input.closest("label.f");
  if (!label) return;
  label.classList.add("invalid");
  const error = document.createElement("small");
  error.className = "error";
  error.textContent = message;
  label.append(error);
  input.focus();
}

async function load() {
  const response = await fetch("/api/settings");
  fill(await response.json());
}

async function save(event) {
  event.preventDefault();
  clearErrors();
  const values = formValues();
  if (!values.local_key) delete values.local_key;
  $("save").disabled = true;
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(values),
    });
    const body = await response.json();
    if (!response.ok) {
      showError(body.field, body.detail || "Settings could not be saved.");
      $("save").disabled = false;
      return;
    }
    fill(body.settings);
    $("saveStatus").className = "ok";
    $("saveStatus").textContent = "Settings saved";
  } catch (_error) {
    showError(null, "LocalAIR could not be reached. Your changes have not been saved.");
    $("save").disabled = false;
  }
}

async function testConnection() {
  const button = $("testButton");
  const result = $("testResult");
  const values = formValues();
  const payload = {
    ip_address: values.ip_address,
    device_id: values.device_id,
    protocol_version: values.protocol_version,
    local_key: values.local_key,
  };
  button.disabled = true;
  result.className = "result";
  result.textContent = "Connecting…";
  try {
    const response = await fetch("/api/settings/test", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) {
      result.className = "result bad";
      result.textContent = body.detail || "The details are not valid.";
    } else if (body.ok) {
      result.className = "result ok";
      result.textContent = `Connected, room is ${body.temperature_c.toFixed(1)} °C and ${body.humidity_percent}%`;
    } else {
      result.className = "result bad";
      result.textContent = body.message;
    }
  } catch (_error) {
    result.className = "result bad";
    result.textContent = "LocalAIR could not be reached.";
  }
  button.disabled = false;
}

form.addEventListener("input", event => {
  const label = event.target.closest("label.f");
  if (label?.classList.contains("invalid")) {
    label.classList.remove("invalid");
    label.querySelector(".error")?.remove();
  }
  update();
});
form.addEventListener("submit", save);
$("discard").addEventListener("click", () => saved && fill(saved));
$("addPeriod").addEventListener("click", addPeriod);
$("testButton").addEventListener("click", testConnection);
$("showKey").addEventListener("click", () => {
  const input = form.local_key;
  input.type = input.type === "password" ? "text" : "password";
  $("showKey").textContent = input.type === "password" ? "Show" : "Hide";
});
window.addEventListener("beforeunload", event => {
  if (changeCount()) event.preventDefault();
});
new ResizeObserver(refreshPeriods).observe($("periods"));
document.addEventListener("themechange", refreshPeriods);

load().catch(() => showError(null, "Settings could not be loaded. Check that LocalAIR is running."));
