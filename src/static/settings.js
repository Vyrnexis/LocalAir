// LocalAIR settings: load, edit, validate, save, and test the sensor.

const NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const form = $("form");
const NUMBER_FIELDS = new Set([
  "turn_on_at", "turn_off_at", "aircon_temperature", "sensor_timeout_seconds",
  "protocol_version", "poll_interval_seconds", "history_retention_days", "server_port",
]);
let saved = null;

function el(parent, tag, attrs = {}, text) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  parent.appendChild(node);
  return node;
}

const inputs = () => [...form.elements].filter(input => input.name);

// Current form values in the shape the API expects.
function formValues() {
  const values = {};
  for (const input of inputs()) {
    if (input.type === "checkbox") values[input.name] = input.checked;
    else if (input.name === "local_key") values.local_key = input.value.trim();
    else if (NUMBER_FIELDS.has(input.name)) values[input.name] = input.value === "" ? "" : Number(input.value);
    else values[input.name] = input.value.trim();
  }
  return values;
}

function fill(settings) {
  saved = settings;
  for (const input of inputs()) {
    if (input.type === "checkbox") input.checked = Boolean(settings[input.name]);
    else if (input.name === "local_key") input.value = "";
    else input.value = settings[input.name] ?? "";
  }
  $("keySaved").hidden = !settings.local_key_saved;
  const overridden = Object.entries(settings.env_overrides || {})
    .filter(([, value]) => value)
    .map(([name]) => name === "server_host" ? "host" : "port");
  $("envNote").hidden = overridden.length === 0;
  $("restartNote").hidden = overridden.length === 2;
  if (overridden.length) {
    $("envNoteText").textContent = `Docker is setting the web ${overridden.join(" and ")} (LOCALAIR_HOST, LOCALAIR_PORT), so ${overridden.length === 2 ? "these fields have" : "that field has"} no effect. Change ${overridden.length === 2 ? "them" : "it"} in the Unraid template instead.`;
  }
  clearErrors();
  update();
}

function changedFields() {
  if (!saved) return [];
  const values = formValues();
  return Object.keys(values).filter(name =>
    name === "local_key" ? values.local_key !== "" : values[name] !== saved[name]
  );
}

// Refresh the save bar and band preview after any edit.
function update() {
  const changed = changedFields();
  const status = $("saveStatus");
  if (!status.classList.contains("bad") || changed.length === 0) {
    status.className = "";
    status.innerHTML = changed.length
      ? `<b>${changed.length} unsaved ${changed.length === 1 ? "change" : "changes"}</b>`
      : "No unsaved changes";
  }
  $("save").disabled = changed.length === 0;
  $("discard").disabled = changed.length === 0;
  drawBandPreview();
}

function drawBandPreview() {
  const svg = $("bandPreview");
  while (svg.firstChild) svg.firstChild.remove();
  const on = Number(form.turn_on_at.value);
  const off = Number(form.turn_off_at.value);
  const text = $("bandText");
  const valid = form.turn_on_at.value !== "" && form.turn_off_at.value !== "" && off < on;
  svg.style.display = valid ? "" : "none";
  if (!valid) {
    text.className = "bad";
    text.textContent = "The turn-off temperature must be lower than the turn-on temperature.";
    return;
  }
  text.className = "";
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
  const gradient = el(el(svg, "defs"), "linearGradient", {id: "warmth", x1: 0, x2: 1});
  el(gradient, "stop", {offset: 0, "stop-color": css("--warm"), "stop-opacity": .08});
  el(gradient, "stop", {offset: 1, "stop-color": css("--warm"), "stop-opacity": .6});
  el(svg, "rect", {x: x(lo), y, width: x(hi) - x(lo), height: h, rx: 6, fill: css("--surface-2")});
  el(svg, "rect", {x: x(on), y, width: x(hi) - x(on), height: h, rx: 6, fill: "url(#warmth)"});
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

function clearErrors() {
  form.querySelectorAll(".invalid").forEach(label => label.classList.remove("invalid"));
  form.querySelectorAll(".error").forEach(error => error.remove());
}

function showError(field, message) {
  const status = $("saveStatus");
  status.className = "bad";
  status.textContent = message;
  const input = field && form.elements[field];
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
$("testButton").addEventListener("click", testConnection);
$("showKey").addEventListener("click", () => {
  const input = form.local_key;
  input.type = input.type === "password" ? "text" : "password";
  $("showKey").textContent = input.type === "password" ? "Show" : "Hide";
});
window.addEventListener("beforeunload", event => {
  if (changedFields().length) event.preventDefault();
});
new ResizeObserver(drawBandPreview).observe($("bandPreview"));
document.addEventListener("themechange", drawBandPreview);

load().catch(() => showError(null, "Settings could not be loaded. Check that LocalAIR is running."));
