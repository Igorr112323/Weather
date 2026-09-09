const $ = (id) => document.getElementById(id);
const REGION_BOUNDS = [[43.2, 36.1], [47.3, 42.4]];
const KRAI_RECT = [[44.0, 37.0], [46.5, 40.5]];
const OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTRIBUTION = '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';

function describeError(response, body) {
  const detail = body && body.detail;
  if (Array.isArray(detail)) {
    const parts = detail.slice(0, 3).map((item) => {
      const where = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body").join(" → ") : "";
      return (where ? where + ": " : "") + String(item.msg ?? item.message ?? "ошибка поля");
    });
    return "Проверьте форму: " + parts.join("; ");
  }
  const reason = body && (body.error ?? body.message ?? body.body?.error);
  // body.error is used for real reason
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

function fmtPct(v) {
  if (v == null || isNaN(v)) return "—";
  return Math.round(v * 100) + "%";
}
function fmtNum(v, digits = 1) {
  if (v == null || isNaN(v)) return "—";
  return Number(v).toLocaleString("ru-RU", { maximumFractionDigits: digits });
}

let map = null;
let points = [];
let selectedPoint = null;
let selectedMarker = null;
let markersLayer = null;
let crops = [];
let lastPayload = null;
let lastSpec = null;
let currentAbort = null;
let timedOut = false;
let elapsedTimer = null;

function initMap() {
  map = L.map("map", { zoomControl: true, attributionControl: false, minZoom: 6, maxZoom: 12 });
  const rect = L.rectangle(KRAI_RECT, {
    color: "#1c6b3c",
    weight: 1.5,
    fillColor: "#e6f4eb",
    fillOpacity: 0.35,
    dashArray: "6 6",
  }).addTo(map);
  rect.bindTooltip("Краснодарский край · зона расчёта", { sticky: true });
  map.fitBounds(REGION_BOUNDS, { padding: [20, 20] });

  let osmAdded = false;
  const tryOsm = () => {
    if (osmAdded) return;
    if (!navigator.onLine) return;
    try {
      const osm = L.tileLayer(OSM_TILE_URL, { maxZoom: 19, attribution: OSM_ATTRIBUTION, opacity: 0.55 });
      osm.on("tileerror", () => { if (map.hasLayer(osm)) map.removeLayer(osm); });
      osm.addTo(map);
      osmAdded = true;
    } catch {}
  };
  setTimeout(tryOsm, 800);
  window.addEventListener("online", tryOsm);

  map.on("click", (event) => {
    const snapped = nearestGridPoint(event.latlng);
    if (snapped) selectPoint(snapped, true);
  });
}

function nearestPoint(latlng) {
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
function nearestGridPoint(latlng) {
  // alias for compatibility + offline snapping
  return nearestPoint(latlng);
}

function renderPoints() {
  if (!map) return;
  if (markersLayer) map.removeLayer(markersLayer);
  const ms = [];
  for (const p of points) {
    const isSel = selectedPoint && selectedPoint.id === p.id;
    const m = L.circleMarker([p.lat, p.lon], {
      radius: isSel ? 9 : 6,
      color: isSel ? "#17242b" : "#1c6b3c",
      weight: isSel ? 2.5 : 1.2,
      fillColor: isSel ? "#e3b46c" : "#2e8b57",
      fillOpacity: isSel ? 0.95 : 0.6,
    });
    m.bindTooltip(`<b>${p.id}</b> · ${p.lat.toFixed(2)}°N ${p.lon.toFixed(2)}°E`, { direction: "top", offset: L.point(0, -8) });
    m.on("click", (ev) => { L.DomEvent.stop(ev); selectPoint(p, true); });
    ms.push(m);
  }
  markersLayer = L.featureGroup(ms).addTo(map);
  if (ms.length && !selectedPoint) map.fitBounds(markersLayer.getBounds().pad(0.2));
  $("map-status").textContent = `${points.length} точек сетки 0.5° · P01–P28 · офлайн-карта работает · tile.openstreetmap.org fallback`;
}

function selectPoint(p, scroll = false) {
  selectedPoint = p;
  if (selectedMarker) { try { map.removeLayer(selectedMarker); } catch {} }
  selectedMarker = L.circleMarker([p.lat, p.lon], {
    radius: 12, color: "#17242b", weight: 3, fillColor: "#e3b46c", fillOpacity: 0.95,
  }).addTo(map).bringToFront();
  $("selected-info").textContent = `Выбрана ${p.id} · ${p.lat.toFixed(3)}°N ${p.lon.toFixed(3)}°E`;
  const sel = $("point-select");
  if (sel) sel.value = p.id;
  $("status").textContent = `Точка ${p.id} выбрана. Выберите месяц и сорт кукурузы.`;
  renderPoints();
  if (scroll && window.innerWidth <= 1100) document.querySelector(".side")?.scrollIntoView({ behavior: "smooth" });
}

function currentMonth() {
  const d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
}
function nextMonthStr(ym) {
  const [y, m] = ym.split("-").map(Number);
  const total = y * 12 + (m - 1) + 1;
  return String(Math.floor(total / 12)).padStart(4, "0") + "-" + String((total % 12) + 1).padStart(2, "0");
}
function el(tag, cls, txt) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = String(txt);
  return n;
}
function kvTable(obj) {
  const dl = el("dl", "kv");
  for (const [k, v] of Object.entries(obj)) {
    dl.appendChild(el("dt", "", k));
    dl.appendChild(el("dd", "", v));
  }
  return dl;
}
function tercileBar(probs, kind = "t2m") {
  if (!probs) return el("p", "hint", "Нет данных");
  const order = ["below", "normal", "above"];
  const labels = { below: "ниже", normal: "около", above: "выше" };
  const vals = order.map(k => probs[k] ?? 0);
  const wrap = el("div", "tercile");
  const bar = el("div", "tbar " + kind);
  for (let i = 0; i < 3; i++) {
    const s = el("span", order[i], fmtPct(vals[i]));
    s.style.width = (vals[i] * 100).toFixed(1) + "%";
    bar.appendChild(s);
  }
  wrap.appendChild(bar);
  const labs = el("div", "tlabels");
  order.forEach((k, i) => { labs.appendChild(el("span", "", `${labels[k]}: ${fmtPct(vals[i])}`)); });
  wrap.appendChild(labs);
  return wrap;
}
function renderCrops() {
  const box = $("crops-list");
  box.textContent = "";
  if (!crops.length) {
    box.appendChild(el("p", "hint", "Справочник пуст — добавьте гибриды. Есть seed из поставки."));
    return;
  }
  for (const c of crops) {
    const row = el("div", "crop-item");
    const meta = el("div", "meta");
    meta.appendChild(el("div", "name", c.name || "—"));
    const sub = `${c.fao ? "ФАО " + c.fao : ""} ${c.gdd ? "· САТ " + c.gdd + "°" : ""} ${c.vp_days ? "· " + c.vp_days + " дн" : ""} ${c.breeder ? "· " + c.breeder : ""}`.trim();
    meta.appendChild(el("div", "sub", sub || "—"));
    const acts = el("div", "acts");
    const btnUse = el("button", "btn ghost", "Выбрать");
    btnUse.onclick = () => { $("variety-select").value = c.name; $("status").textContent = `Сорт ${c.name} выбран.`; };
    const btnEdit = el("button", "btn ghost", "✎");
    btnEdit.onclick = () => fillCropForm(c);
    const btnDel = el("button", "btn ghost", "✕");
    btnDel.onclick = async () => {
      if (!confirm(`Удалить сорт «${c.name}»?`)) return;
      try { await api(`/api/local/crops/${encodeURIComponent(c.name)}`, { method: "DELETE" }); await loadCrops(); }
      catch (e) { $("c_status").textContent = "Ошибка: " + e.message; }
    };
    acts.append(btnUse, btnEdit, btnDel);
    row.append(meta, acts);
    box.appendChild(row);
  }
}
function fillCropForm(c) {
  $("c_name").value = c.name || "";
  $("c_breeder").value = c.breeder || "";
  $("c_fao").value = c.fao || "";
  $("c_gdd").value = c.gdd || "";
  $("c_vp").value = c.vp_days || "";
  $("c_yield").value = c.yield_t_ha || "";
  $("c_ftol").value = c.frost_tol_c ?? -2;
  $("c_ffat").value = c.frost_fatal_c ?? -3;
  $("c_sow_from").value = c.sow_from || "04-20";
  $("c_sow_to").value = c.sow_to || "05-15";
  $("c_notes").value = c.notes || "";
  document.querySelector(".details")?.setAttribute("open", "open");
  $("c_name").focus();
}
function clearCropForm() {
  $("crop-form").reset();
  $("c_ftol").value = -2;
  $("c_ffat").value = -3;
  $("c_status").textContent = "";
}
async function loadCrops() {
  try {
    const data = await api("/api/local/crops");
    crops = data.crops || [];
    const sel = $("variety-select");
    const cur = sel.value;
    sel.textContent = "";
    sel.appendChild(new Option("— без сорта —", ""));
    for (const c of crops) sel.appendChild(new Option(`${c.name} ${c.fao ? "· ФАО " + c.fao : ""}`, c.name));
    if (cur) sel.value = cur;
    renderCrops();
  } catch (e) {
    $("crops-list").textContent = "Ошибка загрузки сортов: " + e.message;
  }
}
function renderSeasonCard(item, idx) {
  const card = el("div", "report-card");
  const months = (item.months || [item.target || `${item.year}-${String(item.month).padStart(2,"0")}`]).join(", ");
  card.appendChild(el("h3", "", `📅 ${months} · lead ${item.lead ?? idx + 1}`));
  const grid = el("div", "prob-grid");
  if (item.t2m) {
    const b = el("div", "prob-item");
    b.appendChild(el("div", "label", "Температура"));
    const q = item.t2m.quantiles_c || {};
    b.appendChild(el("div", "value", `${fmtNum(q.p50)}°C`));
    b.appendChild(el("div", "hint", `P10–P90: ${fmtNum(q.p10)}…${fmtNum(q.p90)} · норма ${fmtNum(item.t2m.normal_c)}°C`));
    b.appendChild(tercileBar(item.t2m.tercile_probs, "t2m"));
    if (item.t2m.anomaly_c != null) b.appendChild(el("div", "hint", `Аномалия: ${fmtNum(item.t2m.anomaly_c)}°C`));
    if (item.t2m.confidence) {
      const conf = item.t2m.confidence;
      b.appendChild(el("span", "badge " + (conf.no_skill ? "warn" : "ok"), `${conf.level || "—"} · RPSS ${fmtNum(conf.rpss,3)}`));
    }
    grid.appendChild(b);
  }
  if (item.tp) {
    const b = el("div", "prob-item");
    b.appendChild(el("div", "label", "Осадки"));
    const q = item.tp.quantiles_mm || {};
    b.appendChild(el("div", "value", `${fmtNum(q.p50,0)} мм`));
    b.appendChild(el("div", "hint", `P10–P90: ${fmtNum(q.p10,0)}…${fmtNum(q.p90,0)} · норма ${fmtNum(item.tp.normal_mm,0)} мм`));
    b.appendChild(tercileBar(item.tp.tercile_probs, "tp"));
    if (item.tp.confidence) {
      const conf = item.tp.confidence;
      b.appendChild(el("span", "badge " + (conf.no_skill ? "warn" : "ok"), `${conf.level || "—"} · RPSS ${fmtNum(conf.rpss,3)}`));
    }
    grid.appendChild(b);
  }
  card.appendChild(grid);
  if (item.t2m?.analog_years?.length) {
    const analogs = el("div", "hint", "Годы-аналоги: " + item.t2m.analog_years.map(a => `${a.year} (${fmtNum(a.value)}°C, вес ${fmtNum(a.weight,2)})`).join(", "));
    card.appendChild(analogs);
  }
  return card;
}
function renderWhatToDo(what) {
  if (!what || !what.length) return null;
  const card = el("div", "report-card");
  card.appendChild(el("h3", "", "✅ Что делать — топ-3"));
  const grid = el("div", "what-grid");
  for (const w of what.slice(0,3)) {
    const c = el("div", "what-card " + (w.level || "info"));
    c.appendChild(el("div", "act", w.action || "—"));
    c.appendChild(el("div", "reason", w.reason || ""));
    if (w.note) c.appendChild(el("div", "hint", w.note));
    grid.appendChild(c);
  }
  card.appendChild(grid);
  return card;
}
function renderWater(water) {
  if (!water) return null;
  const card = el("div", "report-card");
  card.appendChild(el("h3", "", "💧 Вода и полив"));
  const tbl = {};
  tbl["ET0 (P50)"] = `${fmtNum(water.et0_mm?.p50)} мм`;
  tbl["Осадки (P50)"] = `${fmtNum(water.precip_mm?.p50)} мм`;
  tbl["Дефицит"] = `${fmtNum(water.deficit_mm)} мм`;
  tbl["Полив P50"] = `${fmtNum(water.irrigation_m3_ha?.p50)} м³/га`;
  tbl["Полив сухой P10"] = `${fmtNum(water.irrigation_m3_ha?.p10)} м³/га`;
  card.appendChild(kvTable(tbl));
  if (water.reserve_note) card.appendChild(el("p", "hint", water.reserve_note));
  return card;
}
function renderSAT(sat) {
  if (!sat) return null;
  const card = el("div", "report-card");
  card.appendChild(el("h3", "", "🌡 САТ и GDD"));
  if (sat.gdd?.crops?.length) {
    card.appendChild(el("h4", "", "Сорта из справочника"));
    const table = el("table", "table");
    table.innerHTML = `<thead><tr><th>Сорт</th><th>ФАО</th><th>Нужно</th><th>P(хватит)</th><th>GDD P50</th></tr></thead>`;
    const tbody = el("tbody", "");
    for (const r of sat.gdd.crops.slice(0,8)) {
      const tr = el("tr", "");
      tr.innerHTML = `<td>${r.name}</td><td>${r.fao||"—"}</td><td>${r.need||"—"}</td><td>${r.p_ok!=null?fmtPct(r.p_ok):"—"}</td><td>${r.gdd?.p50||"—"}</td>`;
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    card.appendChild(table);
  }
  return card;
}
function renderFrost(frost) {
  if (!frost) return null;
  const card = el("div", "report-card");
  card.appendChild(el("h3", "", "❄ Заморозки и сев"));
  const tbl = {};
  tbl["Период"] = (frost.span || []).join(" — ");
  tbl["P(заморозок)"] = fmtPct(frost.p_frost_any);
  tbl["P(весенний)"] = fmtPct(frost.p_frost_spring);
  if (frost.last_frost) tbl["Последний заморозок"] = `медиана ${frost.last_frost.median}, P90 ${frost.last_frost.p90}`;
  card.appendChild(kvTable(tbl));
  if (frost.crop) {
    const c = frost.crop;
    const ct = {};
    ct["Сорт"] = c.name || "—";
    ct["Окно"] = `${c.sow_from} – ${c.sow_to}`;
    ct["Безопасная дата"] = c.safe_date || "—";
    ct["Риск на начало"] = c.danger_at_sow_from!=null?fmtPct(c.danger_at_sow_from):"—";
    card.appendChild(kvTable(ct));
    if (c.verdict) card.appendChild(el("p", "hint", c.verdict));
  }
  return card;
}
function renderRisks(risks) {
  if (!risks || !risks.length) return null;
  const card = el("div", "report-card");
  card.appendChild(el("h3", "", "⚠ Риски"));
  for (const r of risks) {
    const div = el("div", "risk");
    const left = el("div", "");
    left.appendChild(el("div", "", `${r.label} · ${r.window||""}`));
    left.appendChild(el("div", "hint", `${r.impact||""}`));
    div.append(left, el("div", "prob", fmtPct(r.prob)));
    card.appendChild(div);
  }
  return card;
}
function renderPhenology(ph) {
  if (!ph || !ph.crops?.length) return null;
  const card = el("div", "report-card");
  card.appendChild(el("h3", "", "🌿 Фенология"));
  for (const crop of ph.crops) {
    card.appendChild(el("h4", "", `${crop.name} — ${crop.verdict}`));
    const table = el("table", "table");
    table.innerHTML = `<thead><tr><th>Стадия</th><th>Окно</th><th>Вероятность</th><th>Действие</th></tr></thead>`;
    const tbody = el("tbody", "");
    for (const s of crop.stages) {
      const tr = el("tr", "");
      tr.innerHTML = `<td>${s.name}</td><td>${s.window}</td><td>${fmtPct(s.prob)}</td><td>${s.action}</td>`;
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    card.appendChild(table);
  }
  return card;
}
function renderHindcast(data) {
  const box = $("summary");
  box.textContent = "";
  const head = el("div", "report-card");
  head.appendChild(el("h3", "", `🔍 Проверка на истории · ${data.start}`));
  head.appendChild(kvTable({ "T попаданий": `${data.summary?.t2m?.hits||0}/${data.summary?.t2m?.total||0}`, "P попаданий": `${data.summary?.tp?.hits||0}/${data.summary?.tp?.total||0}` }));
  box.appendChild(head);
  for (const it of data.items || []) {
    const card = el("div", "report-card");
    card.appendChild(el("h3", "", `${it.year} г., месяц ${it.target_month}`));
    box.appendChild(card);
  }
}
function renderForecast(payload, spec) {
  const box = $("summary");
  box.textContent = "";
  const header = el("div", "report-card");
  header.appendChild(el("h3", "", `🌾 Прогноз · ${selectedPoint?.id||""} · ${payload.start} · ${spec.variety||"без сорта"}`));
  header.appendChild(kvTable({
    "Точка": `${selectedPoint?.id||""} · ${payload.lat?.toFixed(3)}°N`,
    "Выпуск": payload.start,
    "Горизонт": `${payload.horizon} мес · ${payload.mode}`,
    "Сорт": spec.variety || "—",
  }));
  box.appendChild(header);
  const seasons = payload.seasons || payload.months || [];
  for (let i=0;i<seasons.length;i++) box.appendChild(renderSeasonCard(seasons[i], i));
  const agro = payload.agro || {};
  const sections = [renderWhatToDo(agro.what_to_do), renderWater(agro.insight?.water), renderSAT(agro.insight?.sat), renderFrost(agro.insight?.frost), renderRisks(agro.insight?.risks), renderPhenology(agro.phenology)];
  for (const s of sections) if (s) box.appendChild(s);
}

function stopElapsed() {
  if (elapsedTimer !== null) { clearInterval(elapsedTimer); elapsedTimer = null; }
}
function startElapsed(statusEl, prefix) {
  const started = Date.now();
  const tick = () => { statusEl.textContent = prefix + Math.round((Date.now() - started) / 1000) + " с…"; };
  tick();
  stopElapsed();
  elapsedTimer = setInterval(tick, 1000);
}

async function run() {
  const status = $("status");
  const button = $("run");
  const cancel = $("cancel");
  const logBox = $("log");
  if (!selectedPoint) { status.textContent = "Сначала выберите точку: кликните по карте."; $("result").hidden = true; return; }
  const spec = {
    lat: selectedPoint.lat,
    lon: selectedPoint.lon,
    point_id: selectedPoint.id,
    start: $("start").value || currentMonth(),
    horizon: parseInt($("horizon").value, 10),
    mode: parseInt($("horizon").value,10)===1 ? "monthly" : "seasonal",
    season_len: parseInt($("horizon").value,10)===1 ? 1 : 3,
    variety: $("variety-select").value || "",
  };
  const past = spec.start < currentMonth();
  button.disabled = true;
  cancel.hidden = false;
  currentAbort = new AbortController();
  timedOut = false;
  const deadline = setTimeout(() => { timedOut = true; if (currentAbort) currentAbort.abort(); }, 600000);
  startElapsed(status, past ? "Проверка на истории: " : "Расчёт: ");
  logBox.hidden = false;
  logBox.textContent = past ? "Запуск проверки…" : "Запуск прогноза…";
  try {
    const path = past ? "/api/local/hindcast" : "/api/local/forecast";
    const out = await api(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(spec),
      signal: currentAbort.signal,
    });
    if (past) {
      renderHindcast(out);
      status.textContent = "Готово: показана проверка на историю с " + out.start + ".";
    } else {
      lastPayload = out;
      lastSpec = spec;
      renderForecast(out.payload, spec);
      status.textContent = out.cached ? "Готово (результат из кэша)." : "Готово.";
      logBox.textContent = (out.log||[]).join("\n") || "Лог пуст";
    }
    $("result").hidden = false;
  } catch (error) {
    stopElapsed();
    $("result").hidden = true;
    if (error && error.name === "AbortError") {
      status.textContent = timedOut ? "Расчёт длился слишком долго и был остановлен. Попробуйте повторить." : "Расчёт отменён.";
    } else {
      status.textContent = "Ошибка: " + error.message;
      logBox.textContent = error.stack || error.message;
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
  initMap();
  try {
    const grid = await api("/api/region/grid?region=krai");
    points = grid.grid.cells.map((cell) => ({ id: cell.id, lat: cell.lat, lon: cell.lon }));
    const sel = $("point-select");
    sel.textContent = "";
    sel.appendChild(new Option("— выберите —", ""));
    for (const p of points) sel.appendChild(new Option(`${p.id} · ${p.lat.toFixed(2)}°N ${p.lon.toFixed(2)}°E`, p.id));
    renderPoints();
    if (points.length) {
      const first = points[0];
      selectPoint(first);
      sel.value = first.id;
    }
  } catch (error) {
    $("map-status").textContent = "Не удалось загрузить карту точек: " + error.message;
    return;
  }
  try {
    const data = await api("/api/local/inputs");
    const through = data.sources_through || {};
    if (through.fields_monthly) {
      const latest = nextMonthStr(through.fields_monthly);
      $("start").max = latest;
      if (currentMonth() > latest) $("start").value = latest;
    }
  } catch {}
  try {
    const aut = await api("/api/local/autonomy");
    const badge = $("autonomy-badge");
    if (aut.autonomous) { badge.textContent = "✓ автономно · интернет не нужен"; badge.className = "badge ok"; }
    else { badge.textContent = "⚠ неполный бандл"; badge.className = "badge warn"; }
  } catch { $("autonomy-badge").textContent = "офлайн-режим"; }
  await loadCrops();
  $("point-select").addEventListener("change", (e)=>{ const p=points.find(x=>x.id===e.target.value); if(p) selectPoint(p); });
  $("run").addEventListener("click", run);
  $("cancel").addEventListener("click", () => { if (currentAbort) currentAbort.abort(); });
  $("btn-print").addEventListener("click", ()=>window.print());
  $("btn-export").addEventListener("click", ()=>{
    if (!lastPayload) { alert("Сначала сделайте расчёт"); return; }
    const blob = new Blob([JSON.stringify({spec:lastSpec,payload:lastPayload},null,2)], {type:"application/json"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href=url; a.download=`agrocast-${selectedPoint?.id||"point"}-${lastSpec?.start||"forecast"}.json`;
    a.click();
    URL.revokeObjectURL(url);
  });
  $("crop-form").addEventListener("submit", async (ev)=>{
    ev.preventDefault();
    const data = {
      name: $("c_name").value.trim(),
      breeder: $("c_breeder").value.trim(),
      fao: $("c_fao").value ? Number($("c_fao").value) : null,
      gdd: $("c_gdd").value ? Number($("c_gdd").value) : null,
      vp_days: $("c_vp").value ? Number($("c_vp").value) : null,
      yield_t_ha: $("c_yield").value ? Number($("c_yield").value) : null,
      frost_tol_c: $("c_ftol").value ? Number($("c_ftol").value) : -2,
      frost_fatal_c: $("c_ffat").value ? Number($("c_ffat").value) : -3,
      sow_from: $("c_sow_from").value || "04-20",
      sow_to: $("c_sow_to").value || "05-15",
      notes: $("c_notes").value.trim(),
    };
    if (!data.name) { $("c_status").textContent="Название обязательно"; return; }
    $("c_status").textContent="Сохранение…";
    try {
      await api("/api/local/crops", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify(data)});
      $("c_status").textContent="Сохранено ✓";
      clearCropForm();
      await loadCrops();
    } catch (e) { $("c_status").textContent="Ошибка: "+e.message; }
  });
  $("c_clear").addEventListener("click", clearCropForm);
}
init();
