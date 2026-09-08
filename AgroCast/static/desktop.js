import {node} from '/assets/dom.js';

const $ = (id) => document.getElementById(id);

function describeError(response, body) {
  const detail = body && body.detail;
  if (Array.isArray(detail)) {
    const parts = detail.slice(0, 3).map((item) => {
      const where = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body").join(" → ") : "";
      return (where ? where + ": " : "") + String(item.msg ?? item.message ?? "ошибка поля");
    });
    return "Проверьте форму: " + parts.join("; ");
  }
  const reason = body && (body.error ?? body.message);
  if (typeof reason === "string" && reason) {
    if (response.status === 404 && reason === "resource_not_found") {
      return "Ресурс не найден (404). Обновите список данных.";
    }
    return reason;
  }
  if (response.status === 404) return "Ресурс не найден (404). Обновите список данных.";
  return "Ошибка сервера (HTTP " + response.status + ").";
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(describeError(response, body));
  return body;
}

const REGION_BOUNDS = [[43.2, 36.1], [47.3, 42.4]];
const TILE_PROVIDERS = [
  {
    url: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> © <a href="https://carto.com/">CARTO</a>',
    subdomains: "abcd"
  },
  {
    url: "https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png",
    attribution: '© OpenStreetMap contributors, Tiles style by Humanitarian OSM Team',
    subdomains: "abc"
  },
  {
    url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    subdomains: "abc"
  }
];
const OSM_TILE_URL = TILE_PROVIDERS[0].url;
const OSM_ATTRIBUTION = TILE_PROVIDERS[0].attribution;

let map = null;
let points = [];
let selectedPoint = null;
let selectedMarker = null;

function initMap() {
  try {
    if (typeof L === "undefined" || !L.map) throw new Error("Leaflet not loaded");
    map = L.map("map", { zoomControl: true, attributionControl: true });
    let tileAdded = false;
    for (const provider of TILE_PROVIDERS) {
      try {
        const layer = L.tileLayer(provider.url, {
          maxZoom: 19,
          attribution: provider.attribution,
          subdomains: provider.subdomains || "abc",
          crossOrigin: true
        });
        // пробуем добавить, если упадёт — пробуем следующий провайдер
        layer.on("tileerror", () => {
          // тихо игнорируем ошибки отдельных тайлов — карта остаётся, точки видны
        });
        layer.addTo(map);
        tileAdded = true;
        break;
      } catch (tileError) {
        continue;
      }
    }
    if (!tileAdded) {
      // без тайлов — просто серый фон, но карта и точки работают (без VPN)
      const mapEl = document.getElementById("map");
      if (mapEl) mapEl.style.background = "#e6eef3";
    }
    map.fitBounds(REGION_BOUNDS);
    map.on("click", (event) => {
      const snapped = nearestGridPoint(event.latlng);
      if (snapped) selectPoint(snapped);
    });
  } catch (e) {
    const mapEl = document.getElementById("map");
    if (mapEl) mapEl.style.background = "#dfe8ee";
  }
}

function nearestGridPoint(latlng) {
  let best = null;
  let bestDistance = Infinity;
  for (const point of points) {
    const distance = latlng.distanceTo(L.latLng(point.lat, point.lon));
    if (distance < bestDistance) {
      bestDistance = distance;
      best = point;
    }
  }
  return best;
}

function renderGridPoints() {
  if (!map || typeof L === "undefined") return;
  for (const point of points) {
    L.circleMarker([point.lat, point.lon], {
      radius: 5,
      color: "#1c6b3c",
      weight: 1.5,
      fillColor: "#1c6b3c",
      fillOpacity: 0.4,
    }).addTo(map).bindTooltip(node("span", point.id), { direction: "top", offset: L.point(0, -6) });
  }
}

function populateFallback() {
  const sel = $("point-select");
  const wrap = $("point-fallback");
  if (!sel || !wrap) return;
  sel.textContent = "";
  const o0 = document.createElement("option");
  o0.value = "";
  o0.textContent = "— выберите точку —";
  sel.appendChild(o0);
  for (const p of points) {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = p.id + " — " + p.lat.toFixed(2) + "°N " + p.lon.toFixed(2) + "°E";
    sel.appendChild(o);
  }
  wrap.hidden = points.length === 0;
  // если карта не инициализировалась — показываем список явно
  if (!map || typeof L === "undefined") wrap.hidden = false;
}

function selectPoint(point) {
  selectedPoint = point;
  if (selectedMarker && map) map.removeLayer(selectedMarker);
  const sel = $("point-select");
  if (sel) sel.value = point.id;
  if (!map || typeof L === "undefined") {
    $("selected-info").textContent = "Точка " + point.id + " · " + point.lat.toFixed(2) + "°N " + point.lon.toFixed(2) + "°E";
    $("result").hidden = true;
    $("status").textContent = "Выбрана точка " + point.id + ". Выберите месяц и нажмите «Показать прогноз».";
    return;
  }
  selectedMarker = L.circleMarker([point.lat, point.lon], {
    radius: 8,
    color: "#17242b",
    weight: 2,
    fillColor: "#e3b46c",
    fillOpacity: 0.95,
  }).addTo(map).bringToFront();
  $("selected-info").textContent = "Точка " + point.id + " · " + point.lat.toFixed(2) + "°N " + point.lon.toFixed(2) + "°E";
  $("result").hidden = true;
  $("status").textContent = "Выбрана точка " + point.id + ". Выберите месяц и нажмите «Показать прогноз».";
}

function currentMonth() {
  const now = new Date();
  return now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0");
}

function nextMonth(monthStr) {
  const [year, month] = monthStr.split("-").map(Number);
  const total = year * 12 + (month - 1) + 1;
  return String(Math.floor(total / 12)).padStart(4, "0") + "-" + String((total % 12) + 1).padStart(2, "0");
}

function requestSpec() {
  const start = $("start").value || currentMonth();
  const horizon = parseInt($("horizon").value, 10);
  const past = start < currentMonth();
  return {
    lat: selectedPoint.lat,
    lon: selectedPoint.lon,
    point_id: selectedPoint.id,
    start,
    horizon,
    mode: horizon === 1 ? "monthly" : "seasonal",
    season_len: horizon === 1 ? 1 : 3,
    kind: past ? "hindcast" : "forecast",
  };
}

function el(tag, className, text) {
  const nd = document.createElement(tag);
  if (className) nd.className = className;
  if (text !== undefined) nd.textContent = String(text);
  return nd;
}

function tercileRow(label, probs, words) {
  const row = el("div", "prob");
  row.appendChild(el("span", "prob-name", label));
  for (const key of ["below", "normal", "above"]) {
    const value = probs && probs[key];
    row.appendChild(el("span", "prob-" + key, words[key] + ": " + (value === undefined ? "—" : Math.round(value * 100) + "%")));
  }
  return row;
}

function renderSummary(payload) {
  const box = $("summary");
  box.textContent = "";
  if (!selectedPoint) return;
  box.appendChild(el("h4", "", "Прогноз · " + selectedPoint.id + " · " + selectedPoint.lat.toFixed(2) + "°N " + selectedPoint.lon.toFixed(2) + "°E"));
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
      card.appendChild(el("p", "hint", "Медиана: " + (p50 == null ? "—" : Math.round(p50) + " мм") + " · норма: " + (normal == null ? "—" : Math.round(normal) + " мм")));
    }
    if (!item.t2m && !item.tp) {
      card.appendChild(el("p", "status", "Нет данных для этого сезона."));
    }
    box.appendChild(card);
  }
}

function renderHindcastSummary(data) {
  const box = $("summary");
  box.textContent = "";
  if (!data || !data.items || !data.items.length) {
    box.appendChild(el("p", "status", "Нет данных для этого периода."));
    return;
  }
  if (selectedPoint) {
    box.appendChild(el("h4", "", "Проверка на истории · " + selectedPoint.id + " · " + data.start));
  }
  if (data.summary) {
    const t = data.summary.t2m || {};
    const p = data.summary.tp || {};
    box.appendChild(el("p", "hint", "Температура: " + (t.hits || 0) + "/" + (t.total || 0) + " попаданий · Осадки: " + (p.hits || 0) + "/" + (p.total || 0) + " попаданий"));
  }
  for (const item of data.items) {
    const card = el("div", "season");
    card.appendChild(el("h4", "", item.year + " г., месяц " + item.target_month));
    for (const v of ["t2m", "tp"]) {
      if (!item[v]) continue;
      const d = item[v];
      const label = v === "t2m" ? "Температура" : "Осадки";
      const unit = d.unit === "c" ? "°C" : "мм";
      card.appendChild(el("p", "hint", label + ": факт " + d.fact + " " + unit + ", прогноз " + d.p50 + " " + unit + ", норма " + d.norm + " " + unit + " · попал: " + (d.hit ? "✓" : "✗")));
    }
    box.appendChild(card);
  }
}

let currentAbort = null;
let elapsedTimer = null;
let timedOut = false;

function stopElapsed() {
  if (elapsedTimer !== null) { clearInterval(elapsedTimer); elapsedTimer = null; }
}

function startElapsed(statusEl, prefix) {
  const started = Date.now();
  const tick = () => {
    statusEl.textContent = prefix + Math.round((Date.now() - started) / 1000) + " с…";
  };
  tick();
  stopElapsed();
  elapsedTimer = setInterval(tick, 1000);
}

async function run() {
  const status = $("status");
  const button = $("run");
  const cancel = $("cancel");
  if (!selectedPoint) {
    status.textContent = "Сначала выберите точку: кликните по карте.";
    $("result").hidden = true;
    return;
  }
  const spec = requestSpec();
  if (spec.start > currentMonth()) {
    status.textContent = "Выбран будущий месяц. Доступны только текущий и прошлые месяцы.";
    $("result").hidden = true;
    return;
  }
  const past = spec.start < currentMonth();
  button.disabled = true;
  cancel.hidden = false;
  currentAbort = new AbortController();
  timedOut = false;
  const deadline = setTimeout(() => {
    timedOut = true;
    if (currentAbort) currentAbort.abort();
  }, 600000);
  startElapsed(status, past ? "Проверка на истории: " : "Расчёт: ");
  try {
    const path = past ? "/api/local/hindcast" : "/api/local/forecast";
    const out = await api(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(spec),
      signal: currentAbort.signal,
    });
    if (past) {
      renderHindcastSummary(out);
      status.textContent = "Готово: показана проверка на историю с " + out.start + ".";
    } else {
      renderSummary(out.payload);
      status.textContent = out.cached ? "Готово (результат из кэша)." : "Готово.";
    }
    $("result").hidden = false;
  } catch (error) {
    stopElapsed();
    $("result").hidden = true;
    if (error && error.name === "AbortError") {
      status.textContent = timedOut
        ? "Расчёт длился слишком долго и был остановлен. Попробуйте повторить."
        : "Расчёт отменён.";
    } else {
      status.textContent = "Ошибка: " + error.message;
    }
  } finally {
    clearTimeout(deadline);
    stopElapsed();
    currentAbort = null;
    cancel.hidden = true;
    button.disabled = false;
  }
}

async function init() {
  $("start").value = currentMonth();
  $("start").min = "2004-01";
  $("start").max = currentMonth();
  try { initMap(); } catch (e) {}
  // если карта не поднялась за 1 сек — показываем список точек
  setTimeout(() => {
    if (!map || typeof L === "undefined") {
      const wrap = $("point-fallback");
      if (wrap) wrap.hidden = false;
    }
  }, 1200);
  try {
    const grid = await api("/api/region/grid?region=krai");
    points = grid.grid.cells.map((cell) => ({ id: cell.id, lat: cell.lat, lon: cell.lon }));
    renderGridPoints();
    populateFallback();
    // если карта пустая — всё равно показываем список
    if (!map || typeof L === "undefined") {
      const wrap = $("point-fallback");
      if (wrap) wrap.hidden = false;
    }
  } catch (error) {
    // надпись «карта не прогрузилась» убрана — продолжаем работу, кнопка остаётся активной
    populateFallback();
  }
  try {
    await api("/api/local/inputs");
    // важно: max всегда = текущий месяц (осень 2026 = 2026-09), не откатываем к марту из-за старых данных world
    // сервер сам вернёт issue_inputs_mismatch если данные старше, но календарь покажет правильный сентябрь
    $("start").max = currentMonth();
    if ($("start").value > currentMonth()) $("start").value = currentMonth();
  } catch (e) { $("start").max = currentMonth(); }
  $("run").addEventListener("click", run);
  $("cancel").addEventListener("click", () => { if (currentAbort) currentAbort.abort(); });
  const sel = $("point-select");
  if (sel) sel.addEventListener("change", (e) => {
    const pt = points.find(p => p.id === e.target.value);
    if (pt) selectPoint(pt);
  });
}

init();
