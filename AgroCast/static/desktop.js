const $ = (id) => document.getElementById(id);

// --- API helpers ---
function describeError(response, body) {
  const detail = body && (body.detail ?? body.code ?? body.message);
  if (Array.isArray(detail)) {
    const parts = detail.slice(0, 3).map((item) => {
      const where = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body").join(" → ") : "";
      return (where ? where + ": " : "") + String(item.msg ?? item.message ?? "ошибка");
    });
    return parts.join("; ");
  }
  if (typeof detail === "string" && detail) return detail;
  if (response.status === 404) return "Ресурс не найден";
  return "Ошибка сервера (HTTP " + response.status + ")";
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(describeError(response, body));
  return body;
}

// --- Tabs ---
function initTabs() {
  const tabs = document.querySelectorAll(".tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const target = tab.dataset.tab;
      $("forecast-tab").hidden = target !== "forecast";
      $("hindcast-tab").hidden = target !== "hindcast";
      $("reports-tab").hidden = target !== "reports";
      if (target === "reports") loadReports();
    });
  });
}

// --- Coordinate to SVG mapping ---
// Krasnodar Krai bounding box: lat 43.5-47.0, lon 36.5-42.0
const MAP_BOUNDS = { latMin: 43.5, latMax: 47.0, lonMin: 36.5, lonMax: 42.0 };
const SVG_BOUNDS = { x: 50, y: 50, w: 500, h: 320 };

function latLonToSvg(lat, lon) {
  const x = SVG_BOUNDS.x + ((lon - MAP_BOUNDS.lonMin) / (MAP_BOUNDS.lonMax - MAP_BOUNDS.lonMin)) * SVG_BOUNDS.w;
  const y = SVG_BOUNDS.y + ((MAP_BOUNDS.latMax - lat) / (MAP_BOUNDS.latMax - MAP_BOUNDS.latMin)) * SVG_BOUNDS.h;
  return { x, y };
}

// --- Points and Map ---
let points = [];
let selectedPoint = null;
let hindcastSelectedPoint = null;

function createMapPoint(svg, group, point, isSelected, onClick) {
  const pos = latLonToSvg(point.lat, point.lon);
  const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
  g.setAttribute("class", "map-point" + (isSelected ? " selected" : ""));
  g.setAttribute("data-id", point.id);

  const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  circle.setAttribute("cx", pos.x);
  circle.setAttribute("cy", pos.y);
  circle.setAttribute("r", isSelected ? "8" : "6");
  g.appendChild(circle);

  const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
  text.setAttribute("x", pos.x);
  text.setAttribute("y", pos.y - 12);
  text.textContent = point.id;
  g.appendChild(text);

  g.addEventListener("click", (e) => {
    e.stopPropagation();
    onClick(point);
  });

  group.appendChild(g);
  return g;
}

function renderMapPoints(group, allPoints, selected, onClick) {
  group.textContent = "";
  for (const point of allPoints) {
    createMapPoint(null, group, point, selected && selected.id === point.id, onClick);
  }
}

async function loadPoints() {
  const grid = await api("/api/region/grid?region=krai");
  points = grid.grid.cells.map((cell) => ({ id: cell.id, lat: cell.lat, lon: cell.lon }));

  renderMapPoints($("map-points"), points, selectedPoint, (point) => {
    selectedPoint = point;
    renderMapPoints($("map-points"), points, selectedPoint, arguments.callee);
    $("selected-info").textContent = `${point.id} · ${point.lat.toFixed(2)}°N ${point.lon.toFixed(2)}°E`;
  });

  renderMapPoints($("hindcast-map-points"), points, hindcastSelectedPoint, (point) => {
    hindcastSelectedPoint = point;
    renderMapPoints($("hindcast-map-points"), points, hindcastSelectedPoint, arguments.callee);
    $("hindcast-selected-info").textContent = `${point.id} · ${point.lat.toFixed(2)}°N ${point.lon.toFixed(2)}°E`;
  });
}

// Re-render maps with proper closures
function refreshForecastMap() {
  renderMapPoints($("map-points"), points, selectedPoint, selectForecastPoint);
}

function refreshHindcastMap() {
  renderMapPoints($("hindcast-map-points"), points, hindcastSelectedPoint, selectHindcastPoint);
}

function selectForecastPoint(point) {
  selectedPoint = point;
  refreshForecastMap();
  $("selected-info").textContent = `${point.id} · ${point.lat.toFixed(2)}°N ${point.lon.toFixed(2)}°E`;
}

function selectHindcastPoint(point) {
  hindcastSelectedPoint = point;
  refreshHindcastMap();
  $("hindcast-selected-info").textContent = `${point.id} · ${point.lat.toFixed(2)}°N ${point.lon.toFixed(2)}°E`;
}

// Override loadPoints to use proper closures
async function loadPointsInit() {
  const grid = await api("/api/region/grid?region=krai");
  points = grid.grid.cells.map((cell) => ({ id: cell.id, lat: cell.lat, lon: cell.lon }));
  refreshForecastMap();
  refreshHindcastMap();
}

// --- Forecast ---
function currentMonth() {
  const now = new Date();
  return now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0");
}

function nextMonth(monthStr) {
  const [year, month] = monthStr.split("-").map(Number);
  const total = year * 12 + (month - 1) + 1;
  return String(Math.floor(total / 12)).padStart(4, "0") + "-" + String((total % 12) + 1).padStart(2, "0");
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function tercileRow(label, probs, words) {
  const row = el("div", "prob");
  row.appendChild(el("span", "prob-name", label));
  for (const key of ["below", "normal", "above"]) {
    const value = probs && probs[key];
    row.appendChild(el("span", "prob-" + key, `${words[key]}: ${value === undefined ? "—" : Math.round(value * 100) + "%"}`));
  }
  return row;
}

function renderSummary(payload) {
  const box = $("summary");
  box.textContent = "";
  if (!selectedPoint) return;
  box.appendChild(el("h4", "", `${selectedPoint.id} · ${selectedPoint.lat.toFixed(2)}°N ${selectedPoint.lon.toFixed(2)}°E`));
  const seasons = payload.seasons || [];
  if (!seasons.length) {
    box.appendChild(el("p", "status", "Нет данных для этого периода."));
    return;
  }
  for (const item of seasons) {
    const card = el("div", "season");
    const months = (item.months || [item.target || ""]).join(", ");
    card.appendChild(el("h4", "", months));
    if (item.t2m && item.t2m.tercile_probs) {
      card.appendChild(tercileRow("Температура", item.t2m.tercile_probs, { below: "ниже нормы", normal: "около нормы", above: "выше нормы" }));
    }
    if (item.tp && item.tp.tercile_probs) {
      card.appendChild(tercileRow("Осадки", item.tp.tercile_probs, { below: "меньше нормы", normal: "около нормы", above: "больше нормы" }));
      const p50 = item.tp.quantiles_mm && item.tp.quantiles_mm.p50;
      const normal = item.tp.normal_mm;
      card.appendChild(el("p", "hint", `Медиана: ${p50 == null ? "—" : Math.round(p50) + " мм"} · норма: ${normal == null ? "—" : Math.round(normal) + " мм"}`));
    }
    if (!item.t2m && !item.tp) {
      card.appendChild(el("p", "status", "Нет данных для этого сезона."));
    }
    box.appendChild(card);
  }
}

let currentAbort = null;
let elapsedTimer = null;

function stopElapsed() {
  if (elapsedTimer !== null) { clearInterval(elapsedTimer); elapsedTimer = null; }
}

function startElapsed(statusEl) {
  const started = Date.now();
  const tick = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    statusEl.textContent = "Расчёт: " + seconds + " с…";
  };
  tick();
  stopElapsed();
  elapsedTimer = setInterval(tick, 1000);
}

async function runForecast() {
  const status = $("status");
  const button = $("run");
  const cancel = $("cancel");

  if (!selectedPoint) {
    status.textContent = "Сначала выберите точку на карте.";
    return;
  }

  const start = $("start").value || currentMonth();
  button.disabled = true;
  cancel.hidden = false;
  currentAbort = new AbortController();
  startElapsed(status);

  try {
    const out = await api("/api/local/forecast", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        lat: selectedPoint.lat,
        lon: selectedPoint.lon,
        point_id: selectedPoint.id,
        start,
        horizon: 3,
        mode: "seasonal",
        season_len: 3,
      }),
      signal: currentAbort.signal,
    });
    renderSummary(out.payload);
    $("result").hidden = false;
    status.textContent = out.cached ? "Результат из кэша." : "Готово.";
  } catch (error) {
    stopElapsed();
    if (error && error.name === "AbortError") {
      status.textContent = "Расчёт отменён.";
    } else {
      status.textContent = "Ошибка: " + error.message;
    }
  } finally {
    stopElapsed();
    currentAbort = null;
    cancel.hidden = true;
    button.disabled = false;
  }
}

// --- Hindcast ---
function renderHindcastSummary(data) {
  const box = $("hindcast-summary");
  box.textContent = "";
  if (!data || !data.items || !data.items.length) {
    box.appendChild(el("p", "status", "Нет данных для этого периода."));
    return;
  }
  if (hindcastSelectedPoint) {
    box.appendChild(el("h4", "", `${hindcastSelectedPoint.id} · ${data.start}`));
  }
  if (data.summary) {
    const t = data.summary.t2m || {};
    const p = data.summary.tp || {};
    box.appendChild(el("p", "hint", `Температура: ${t.hits || 0}/${t.total || 0} попаданий · Осадки: ${p.hits || 0}/${p.total || 0} попаданий`));
  }
  for (const item of data.items) {
    const card = el("div", "season");
    card.appendChild(el("h4", "", `${item.year} г., месяц ${item.target_month}`));
    for (const v of ["t2m", "tp"]) {
      if (!item[v]) continue;
      const d = item[v];
      const label = v === "t2m" ? "Температура" : "Осадки";
      const unit = d.unit === "c" ? "°C" : "мм";
      card.appendChild(el("p", "hint", `${label}: факт ${d.fact} ${unit}, прогноз ${d.p50} ${unit}, норма ${d.norm} ${unit} · попал: ${d.hit ? "✓" : "✗"}`));
    }
    box.appendChild(card);
  }
}

async function runHindcast() {
  const status = $("hindcast-status");
  const button = $("hindcast-run");

  if (!hindcastSelectedPoint) {
    status.textContent = "Сначала выберите точку на карте.";
    return;
  }

  const year = parseInt($("hindcast-start").value, 10);
  if (!year || year < 2004 || year > 2024) {
    status.textContent = "Выберите год от 2004 до 2024.";
    return;
  }

  button.disabled = true;
  status.textContent = "Проверяю…";

  try {
    const out = await api("/api/local/hindcast", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        lat: hindcastSelectedPoint.lat,
        lon: hindcastSelectedPoint.lon,
        point_id: hindcastSelectedPoint.id,
        start: year + "-01",
        horizon: 6,
        mode: "seasonal",
        season_len: 3,
        kind: "hindcast",
      }),
    });
    renderHindcastSummary(out);
    $("hindcast-result").hidden = false;
    status.textContent = "Готово.";
  } catch (error) {
    status.textContent = "Ошибка: " + error.message;
  } finally {
    button.disabled = false;
  }
}

// --- Reports tab ---
async function loadReports() {
  const container = $("reports-list");
  const cacheInfo = $("cache-info");
  try {
    const data = await api("/api/local/inputs");
    const entries = data.results_cache.entries || [];
    container.textContent = "";
    if (!entries.length) {
      container.appendChild(el("p", "hint", "Пока нет сохранённых расчётов."));
      return;
    }
    container.appendChild(el("p", "hint", `Сохранено расчётов: ${data.results_cache.total}`));
    for (const entry of entries) {
      const row = el("div", "season");
      const ts = entry.stored_at ? new Date(entry.stored_at * 1000).toLocaleDateString("ru-RU") : "—";
      row.appendChild(el("p", "hint", `${entry.key} · ${ts}`));
      container.appendChild(row);
    }
  } catch (error) {
    container.textContent = "";
    container.appendChild(el("p", "status", "Не удалось загрузить: " + error.message));
  }
}

// --- Init ---
async function init() {
  initTabs();
  $("start").value = currentMonth();

  try {
    await loadPointsInit();
  } catch (error) {
    $("status").textContent = "Не удалось загрузить точки: " + error.message;
    $("hindcast-status").textContent = "Не удалось загрузить точки: " + error.message;
    return;
  }

  // Set max month based on data availability
  try {
    const data = await api("/api/local/inputs");
    const through = data.sources_through || {};
    if (through.fields_monthly) {
      const latest = nextMonth(through.fields_monthly);
      $("start").max = latest;
      if (currentMonth() > latest) $("start").value = latest;
    }
  } catch (e) { /* non-critical */ }

  $("run").addEventListener("click", runForecast);
  $("cancel").addEventListener("click", () => { if (currentAbort) currentAbort.abort(); });
  $("hindcast-run").addEventListener("click", runHindcast);
}

init();
