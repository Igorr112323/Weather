const $ = (id) => document.getElementById(id);

function describeError(response, body) {
  const detail = body && (body.detail ?? body.code ?? body.message);
  if (Array.isArray(detail)) {
    const parts = detail.slice(0, 3).map((item) => {
      const where = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(" → ") : "";
      return (where ? where + ": " : "") + String(item.msg ?? item.message ?? "ошибка поля");
    });
    return "Проверьте форму: " + parts.join("; ");
  }
  if (typeof detail === "string" && detail) return detail;
  if (response.status === 404) return "Ресурс не найден (404). Обновите список данных.";
  return "Ошибка сервера (HTTP " + response.status + ").";
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(describeError(response, body));
  return body;
}

function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

function fmtSize(bytes) {
  if (!bytes) return "0 КБ";
  if (bytes > 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " МБ";
  return Math.max(1, Math.round(bytes / 1024)) + " КБ";
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function nextMonth(monthStr) {
  const [year, month] = monthStr.split("-").map(Number);
  const total = year * 12 + (month - 1) + 1;
  return String(Math.floor(total / 12)).padStart(4, "0") + "-" + String((total % 12) + 1).padStart(2, "0");
}

function currentMonth() {
  const now = new Date();
  return now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0");
}

let points = [];

async function loadPoints() {
  const grid = await api("/api/region/grid?region=krai");
  points = grid.grid.cells.map((cell) => ({ id: cell.id, lat: cell.lat, lon: cell.lon }));
  const select = $("point");
  select.textContent = "";
  for (const point of points) {
    const option = el("option", "", `${point.id} · ${point.lat.toFixed(2)}°N ${point.lon.toFixed(2)}°E`);
    option.value = point.id;
    select.appendChild(option);
  }
  if (!points.length) {
    const empty = el("option", "", "нет доступных точек — обновите данные");
    empty.disabled = true;
    empty.selected = true;
    select.appendChild(empty);
    $("run").disabled = true;
  } else {
    $("run").disabled = false;
  }
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
  const seasons = payload.seasons || [];
  if (!seasons.length) {
    box.appendChild(el("p", "status", "Ответ не содержит сезонных блоков."));
    return;
  }
  for (const item of seasons) {
    const card = el("div", "season");
    const months = (item.months || [item.target || ""]).join(", ");
    card.appendChild(el("h4", "", `${months}`));
    if (!item.t2m && !item.tp) {
      card.appendChild(el("p", "status", "Для этого сезона нет ни температуры, ни осадков — расчёт завершился частично."));
    }
    if (!item.tp && item.t2m) {
      card.appendChild(el("p", "hint", "Осадки для этого сезона не рассчитаны — показана только температура."));
    }
    if (!item.t2m && item.tp) {
      card.appendChild(el("p", "hint", "Температура для этого сезона не рассчитана — показаны только осадки."));
    }
    if (item.t2m && item.t2m.tercile_probs) {
      card.appendChild(tercileRow("Температура", item.t2m.tercile_probs, { below: "ниже нормы", normal: "около нормы", above: "выше нормы" }));
    }
    if (item.tp && item.tp.tercile_probs) {
      card.appendChild(tercileRow("Осадки", item.tp.tercile_probs, { below: "меньше нормы", normal: "около нормы", above: "больше нормы" }));
      const p50 = item.tp.quantiles_mm && item.tp.quantiles_mm.p50;
      const normal = item.tp.normal_mm;
      card.appendChild(el("p", "hint", `Медиана осадков: ${p50 === undefined || p50 === null ? "—" : Math.round(p50) + " мм"} · норма: ${normal === undefined || normal === null ? "—" : Math.round(normal) + " мм"}`));
    }
    if (item.issue_through || payload.issue_data_through) {
      card.appendChild(el("p", "hint", `Данные наблюдений включены до: ${item.issue_through || payload.issue_data_through}`));
    }
    box.appendChild(card);
  }
  $("raw").textContent = JSON.stringify(payload, null, 1);
}

const FORECAST_DEADLINE_MS = 600000;
let elapsedTimer = null;
let currentAbort = null;

function stopElapsed() {
  if (elapsedTimer !== null) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function startElapsed(status) {
  const started = Date.now();
  const tick = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    status.textContent = "Считаю локально: " + seconds + " с. Первый расчёт новой точки докачает наблюдения — это 2–6 минут, один раз.";
  };
  tick();
  stopElapsed();
  elapsedTimer = setInterval(tick, 1000);
}

async function runForecast() {
  const button = $("run");
  const cancel = $("cancel");
  const status = $("status");
  const pointId = $("point").value;
  const point = points.find((item) => item.id === pointId) || {};
  const start = $("start").value || currentMonth();
  button.disabled = true;
  cancel.hidden = false;
  currentAbort = new AbortController();
  let timedOut = false;
  const deadline = setTimeout(() => {
    timedOut = true;
    currentAbort && currentAbort.abort();
  }, FORECAST_DEADLINE_MS);
  cancel.onclick = () => {
    currentAbort && currentAbort.abort();
  };
  startElapsed(status);
  try {
    const out = await api("/api/local/forecast", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ lat: point.lat, lon: point.lon, point_id: point.id, start, horizon: 3, mode: "seasonal", season_len: 3 }),
      signal: currentAbort.signal,
    });
    renderSummary(out.payload);
    $("result-meta").textContent = out.cached
      ? "Взято из локального кэша — расчёт не запускался."
      : "Рассчитано сейчас на этом компьютере и сохранено в кэш.";
    $("result").hidden = false;
    status.textContent = "Готово.";
    loadInputs();
  } catch (error) {
    stopElapsed();
    if (error && error.name === "AbortError") {
      status.textContent = timedOut
        ? "Расчёт длился больше 10 минут и был остановлен. Уже скачанные данные сохранены — попробуйте повторить."
        : "Расчёт отменён. Скачанные данные и кэш сохранены.";
    } else {
      status.textContent = "Не получилось: " + error.message;
    }
  } finally {
    clearTimeout(deadline);
    stopElapsed();
    currentAbort = null;
    cancel.hidden = true;
    button.disabled = false;
  }
}

function dataCard(title, subtitle) {
  const card = el("section", "data-card");
  card.appendChild(el("h3", "", title));
  card.appendChild(el("p", "hint", subtitle));
  return card;
}

function fileTable(files, limit = 9) {
  const list = el("ul", "files");
  for (const file of files.slice(0, limit)) {
    list.appendChild(el("li", "", `${file.path} · ${fmtSize(file.size_bytes)} · ${fmtDate(file.modified_at)}`));
  }
  if (files.length > limit) list.appendChild(el("li", "hint", `… и ещё ${files.length - limit}`));
  return list;
}

async function loadInputs() {
  const status = $("data-status");
  try {
    const data = await api("/api/local/inputs");
    $("state-dir").textContent = data.state_dir;
    const box = $("data");
    box.textContent = "";
    const bundle = dataCard("Локальный набор моделей", `${data.bundle.file_count} файлов · ${fmtSize(data.bundle.total_bytes)}`);
    const manifest = data.bundle_manifest || {};
    if (manifest.configuration && manifest.configuration.train_start) bundle.appendChild(el("p", "hint", "Климатическая база обучения: " + manifest.configuration.train_start + "–" + (manifest.configuration.backtest_start || "…")));
    if (data.releases) bundle.appendChild(el("p", "hint", "Отпечаток данных выпуска: " + String(data.releases.data_release).slice(0, 12) + "…"));
    const through = data.sources_through || {};
    if (through.fields_monthly) {
      const latest = nextMonth(through.fields_monthly);
      $("start").max = latest;
      if (currentMonth() > latest) $("start").value = latest;
      bundle.appendChild(el("p", "hint", "Предсказорные входы есть до " + through.fields_monthly + " включительно; прогноз можно запрашивать до " + latest + " — позднее этого месяца расчёт не публикуется."));
    }
    if (through.daily_region) bundle.appendChild(el("p", "hint", "Суточные поля обновлены до " + through.daily_region + "."));
    bundle.appendChild(fileTable(data.bundle.files, 6));
    box.appendChild(bundle);
    const obs = dataCard("Скачанные наблюдения (для прогноза)", `${data.observations.file_count} файлов · ${fmtSize(data.observations.total_bytes)}`);
    obs.appendChild(el("p", "hint", data.observations.file_count ? "Суточные ряды наблюдений, загруженные на этом компьютере." : "Пока пусто — закачаются при первом расчёте новой точки."));
    obs.appendChild(fileTable(data.observations.files, 8));
    box.appendChild(obs);
    const cache = dataCard("Кэш результатов", `${data.results_cache.total} сохранённых расчётов`);
    const entries = data.results_cache.entries || [];
    if (!entries.length) cache.appendChild(el("p", "hint", "Ещё ничего не рассчитано."));
    for (const entry of entries.slice(0, 8)) {
      cache.appendChild(el("p", "hint", `${String(entry.key).slice(0, 16)}… · ${fmtSize(entry.size_bytes)} · ${fmtDate(entry.stored_at)}`));
    }
    box.appendChild(cache);
    const ageMinutes = Math.round((Date.now() / 1000 - Number(data.generated_at || 0)) / 60);
    const staleHint = Number.isFinite(ageMinutes) && ageMinutes > 10 ? " Список старше " + ageMinutes + " минут — нажмите «Обновить»." : "";
    status.textContent = "Обновлено " + fmtDate(data.generated_at) + "." + staleHint;
  } catch (error) {
    $("data").textContent = "";
    $("data").appendChild(el("p", "status", "Список данных недоступен: " + error.message));
    status.textContent = "Не удалось прочитать список данных: " + error.message;
  }
}

$("start").value = currentMonth();
$("run").addEventListener("click", runForecast);
$("refresh").addEventListener("click", loadInputs);
loadPoints().catch((error) => { $("status").textContent = "Не удалось загрузить точки: " + error.message; });
loadInputs();
