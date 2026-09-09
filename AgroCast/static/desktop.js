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
    url: "https://{s}.tile.openstreetmap.de/{z}/{x}/{y}.png",
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    subdomains: "abc"
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
    map = L.map("map", { zoomControl: true, attributionControl: true, minZoom: 6, maxZoom: 12 });
    let tileAdded = false;
    for (const provider of TILE_PROVIDERS) {
      try {
        const layer = L.tileLayer(provider.url, {
          maxZoom: 19,
          attribution: provider.attribution,
          subdomains: provider.subdomains || "abc",
          crossOrigin: true
        });
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
      const mapEl = document.getElementById("map");
      if (mapEl) mapEl.style.background = "#e6eef3";
    }
    // Krasnodar focus — force fit after tiles, fix white-screen world view
    map.fitBounds(REGION_BOUNDS);
    setTimeout(() => {
      try { map.invalidateSize(); map.fitBounds(REGION_BOUNDS); } catch(e) {}
    }, 300);
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
  // всегда показываем список — карта может не прогрузиться, а точки выбрать нужно
  wrap.hidden = points.length === 0;
  if (points.length > 0) wrap.hidden = false;
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

function isHindcastMonth(start) {
  // hindcast только для истории 2004-2024, иначе — прогноз (даже если дата в прошлом относительно сегодня)
  return start >= "2004-01" && start <= "2024-12" && start < currentMonth();
}
function requestSpec() {
  const start = $("start").value || currentMonth();
  const horizon = parseInt($("horizon").value, 10);
  const past = isHindcastMonth(start);
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
  const wrap = el("div", "");
  const row = el("div", "prob");
  row.appendChild(el("span", "prob-name", label));
  const vals = {};
  for (const key of ["below", "normal", "above"]) {
    const value = probs && probs[key];
    vals[key] = value;
    row.appendChild(el("span", "prob-" + key, words[key] + ": " + (value === undefined ? "—" : Math.round(value * 100) + "%")));
  }
  wrap.appendChild(row);
  // цветная полоска — наглядно: синяя/серая/оранжевая = терцили
  const bar = el("div", "bar-track");
  bar.setAttribute("role","img");
  bar.setAttribute("aria-label", `${label}: ниже ${Math.round((vals.below||0)*100)}% норма ${Math.round((vals.normal||0)*100)}% выше ${Math.round((vals.above||0)*100)}%`);
  for (const key of ["below","normal","above"]) {
    const seg = el("div", "bar-" + key + " bar-segment");
    const w = vals[key] == null ? 0 : Math.max(0, Math.min(1, vals[key])) * 100;
    seg.style.width = w.toFixed(1) + "%";
    bar.appendChild(seg);
  }
  wrap.appendChild(bar);
  return wrap;
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
    const head = el("h4", "", item.year + " г., месяц " + item.target_month);
    card.appendChild(head);
    for (const v of ["t2m", "tp"]) {
      if (!item[v]) continue;
      const d = item[v];
      const label = v === "t2m" ? "Температура" : "Осадки";
      const unit = d.unit === "c" ? "°C" : "мм";
      const p = el("p", "hint", label + ": факт " + d.fact + " " + unit + " · прогноз " + d.p50 + " " + unit + " · норма " + d.norm + " " + unit + " ");
      const badge = el("span", d.hit ? "hind-hit ok" : "hind-hit bad", d.hit ? "✓ попал" : "✗ мимо");
      p.appendChild(badge);
      card.appendChild(p);
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
    // пробуем взять из списка если карта не кликалась
    const sel = $("point-select");
    if (sel && sel.value) {
      const pt = points.find(p => p.id === sel.value);
      if (pt) selectedPoint = pt;
    }
    if (!selectedPoint) {
      status.textContent = "Сначала выберите точку: кликните по карте или выберите из списка.";
      $("result").hidden = true;
      return;
    }
  }
  const spec = requestSpec();
  if (spec.start > currentMonth()) {
    status.textContent = "Выбран будущий месяц. Доступны только текущий и прошлые месяцы.";
    $("result").hidden = true;
    return;
  }
  if (spec.start < "2004-01") {
    status.textContent = "Дата слишком ранняя. Доступно с 2004-01.";
    $("result").hidden = true;
    return;
  }
  const past = isHindcastMonth(spec.start);
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

const FALLBACK_POINTS = [
  {id:"P01",lat:46.25,lon:38.25},{id:"P02",lat:46.25,lon:38.75},{id:"P03",lat:46.25,lon:39.25},{id:"P04",lat:46.25,lon:39.75},{id:"P05",lat:46.25,lon:40.25},
  {id:"P06",lat:45.75,lon:37.75},{id:"P07",lat:45.75,lon:38.25},{id:"P08",lat:45.75,lon:38.75},{id:"P09",lat:45.75,lon:39.25},{id:"P10",lat:45.75,lon:39.75},{id:"P11",lat:45.75,lon:40.25},
  {id:"P12",lat:45.25,lon:37.25},{id:"P13",lat:45.25,lon:37.75},{id:"P14",lat:45.25,lon:38.25},{id:"P15",lat:45.25,lon:38.75},{id:"P16",lat:45.25,lon:39.25},{id:"P17",lat:45.25,lon:39.75},{id:"P18",lat:45.25,lon:40.25},
  {id:"P19",lat:44.75,lon:37.75},{id:"P20",lat:44.75,lon:38.25},{id:"P21",lat:44.75,lon:38.75},{id:"P22",lat:44.75,lon:39.25},{id:"P23",lat:44.75,lon:39.75},{id:"P24",lat:44.75,lon:40.25},
  {id:"P25",lat:44.25,lon:38.75},{id:"P26",lat:44.25,lon:39.25},{id:"P27",lat:44.25,lon:39.75},{id:"P28",lat:44.25,lon:40.25}
];

function initTabs() {
  const tabs = document.querySelectorAll(".tab");
  const panels = document.querySelectorAll(".tab-panel");
  tabs.forEach(tab => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.tab;
      tabs.forEach(t => { t.classList.remove("active"); t.setAttribute("aria-selected","false"); });
      tab.classList.add("active"); tab.setAttribute("aria-selected","true");
      panels.forEach(p => {
        const isTarget = p.id === "tab-" + target;
        p.hidden = !isTarget;
        if (isTarget) p.classList.add("active"); else p.classList.remove("active");
      });
      if (target === "forecast" && map) {
        setTimeout(() => { try { map.invalidateSize(); map.fitBounds(REGION_BOUNDS); } catch(e) {} }, 100);
      }
      if (target === "downloads") loadDownloads();
      if (target === "crops") loadCrops();
    });
  });
}

async function loadDownloads() {
  const bundleInfo = $("bundle-info");
  const bundleFiles = $("bundle-files");
  const cacheInfo = $("cache-info");
  const cacheFiles = $("cache-files");
  const sourcesThrough = $("sources-through");
  if (!bundleInfo) return;
  try {
    const data = await api("/api/local/inputs");
    bundleInfo.textContent = `Файлов: ${data.bundle.file_count}, байт: ${data.bundle.total_bytes}, мир: ${data.world_dir}`;
    bundleFiles.textContent = "";
    const btbl = document.createElement("table");
    btbl.innerHTML = "<tr><th>Файл</th><th>Размер</th></tr>" + data.bundle.files.slice(0,100).map(f=>`<tr><td>${f.path}</td><td>${f.size_bytes}</td></tr>`).join("");
    bundleFiles.appendChild(btbl);
    cacheInfo.textContent = `Кэш результатов: ${data.results_cache.total} файлов`;
    cacheFiles.textContent = "";
    if (data.results_cache.entries && data.results_cache.entries.length) {
      const ctbl = document.createElement("table");
      ctbl.innerHTML = "<tr><th>Ключ</th><th>Размер</th></tr>" + data.results_cache.entries.map(e=>`<tr><td>${e.key}</td><td>${e.size_bytes}</td></tr>`).join("");
      cacheFiles.appendChild(ctbl);
    } else {
      cacheFiles.textContent = "Кэш пуст — запустите прогноз";
    }
    sourcesThrough.textContent = "";
    if (data.sources_through) {
      const stbl = document.createElement("table");
      stbl.innerHTML = "<tr><th>Источник</th><th>До</th></tr>" + Object.entries(data.sources_through).map(([k,v])=>`<tr><td>${k}</td><td>${v||"—"}</td></tr>`).join("");
      sourcesThrough.appendChild(stbl);
    }
  } catch(e) {
    bundleInfo.textContent = "Ошибка: " + e.message;
  }
}

let cropsCache = [];
async function loadCrops() {
  const list = $("crops-list");
  const status = $("c_status");
  if (!list) return;
  try {
    const data = await api("/api/crops?limit=100");
    cropsCache = data.crops || [];
    list.textContent = "";
    if (!cropsCache.length) {
      list.textContent = "Сортов пока нет — добавьте ниже. День, САТ, ФАО — всё как писали.";
      return;
    }
    const tbl = document.createElement("table");
    tbl.innerHTML = "<tr><th>Сорт</th><th>Селекционер</th><th>ФАО</th><th>САТ</th><th>Дней</th><th>Сев</th><th>Урожай</th><th></th></tr>" + cropsCache.map(c=>`<tr><td>${c.name||c.id}</td><td>${c.breeder||"—"}</td><td>${c.fao||"—"}</td><td>${c.gdd||"—"}</td><td>${c.vegetation_days||c.vp||"—"}</td><td>${(c.sow_from||"")+ "–"+(c.sow_to||"")}</td><td>${c.yield_t_ha||"—"}</td><td><button data-id="${c.id}" class="secondary c-edit">Открыть</button></td></tr>`).join("");
    list.appendChild(tbl);
    list.querySelectorAll(".c-edit").forEach(btn=> btn.addEventListener("click", ()=> {
      const c = cropsCache.find(x=> x.id===btn.dataset.id);
      if(c) fillCropForm(c);
    }));
  } catch(e) {
    list.textContent = "Ошибка: " + e.message;
  }
}
function fillCropForm(c) {
  $("c_name").value = c.name||"";
  $("c_breeder").value = c.breeder||"";
  $("c_maturity").value = c.maturity_group||c.maturity||"";
  $("c_fao").value = c.fao||"";
  $("c_gdd").value = c.gdd||"";
  $("c_vp").value = c.vegetation_days||c.vp||"";
  $("c_sow_from").value = c.sow_from||"";
  $("c_sow_to").value = c.sow_to||"";
  $("c_area").value = c.area_ha||c.area||"";
  $("c_yield").value = c.yield_t_ha||c.yield||"";
  $("c_ftol").value = c.frost_toler||c.ftol||"-2";
  $("c_ffat").value = c.frost_fatal||c.ffat||"-3";
  $("c_notes").value = c.notes||"";
  $("c_delete").hidden = false;
  $("c_delete").dataset.id = c.id;
  $("c_status").textContent = "Открыт сорт " + (c.name||c.id) + " — измените и Сохранить";
}

async function init() {
  $("start").value = currentMonth();
  $("start").min = "2004-01";
  $("start").max = currentMonth();
  try { initMap(); } catch (e) {}
  // fallback всегда виден — карта может не грузиться, но точки выбрать нужно
  // показываем список сразу, даже до загрузки grid
  points = FALLBACK_POINTS.slice();
  populateFallback();
  renderGridPoints();
  setTimeout(() => {
    const wrap = $("point-fallback");
    if (wrap) wrap.hidden = false;
  }, 500);
  try {
    const grid = await api("/api/region/grid?region=krai");
    points = grid.grid.cells.map((cell) => ({ id: cell.id, lat: cell.lat, lon: cell.lon }));
    renderGridPoints();
    populateFallback();
  } catch (error) {
    // API не ответил — используем FALLBACK_POINTS, список уже показан
    points = FALLBACK_POINTS.slice();
    populateFallback();
    renderGridPoints();
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
  // вкладки
  initTabs();
  // форма кукурузы
  const cropForm = $("crop-form");
  if (cropForm) {
    cropForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = {
        name: $("c_name").value.trim(),
        breeder: $("c_breeder").value.trim(),
        maturity_group: $("c_maturity").value.trim(),
        fao: $("c_fao").value ? parseInt($("c_fao").value,10) : null,
        gdd: $("c_gdd").value ? parseFloat($("c_gdd").value) : null,
        vegetation_days: $("c_vp").value ? parseInt($("c_vp").value,10) : null,
        sow_from: $("c_sow_from").value.trim(),
        sow_to: $("c_sow_to").value.trim(),
        area_ha: $("c_area").value ? parseFloat($("c_area").value) : null,
        yield_t_ha: $("c_yield").value ? parseFloat($("c_yield").value) : null,
        frost_toler: $("c_ftol").value ? parseFloat($("c_ftol").value) : null,
        frost_fatal: $("c_ffat").value ? parseFloat($("c_ffat").value) : null,
        notes: $("c_notes").value.trim()
      };
      const isEdit = !$("c_delete").hidden && $("c_delete").dataset.id;
      try {
        if (isEdit) {
          await api(`/api/crops/${$("c_delete").dataset.id}`, {method:"PUT", headers:{"content-type":"application/json"}, body:JSON.stringify(payload)});
          $("c_status").textContent = "Сохранено";
        } else {
          await api("/api/crops", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify(payload)});
          $("c_status").textContent = "Сорт добавлен";
        }
        cropForm.reset();
        $("c_delete").hidden = true;
        loadCrops();
      } catch(err) { $("c_status").textContent = "Ошибка: " + err.message; }
    });
    $("c_clear").addEventListener("click", ()=> { cropForm.reset(); $("c_delete").hidden=true; $("c_status").textContent=""; });
    $("c_delete").addEventListener("click", async ()=> {
      const id = $("c_delete").dataset.id;
      if (!id || !confirm("Удалить сорт?")) return;
      try { await api(`/api/crops/${id}`, {method:"DELETE"}); $("c_status").textContent="Удалён"; cropForm.reset(); $("c_delete").hidden=true; loadCrops(); } catch(err){ $("c_status").textContent="Ошибка: "+err.message; }
    });
  }
  const printBtn = document.getElementById("print-report");
  if (printBtn) printBtn.addEventListener("click", () => window.print());
}

init();
