import {byId, fillTable, finite, list, node, number, percent, record, signed, table, text} from '/assets/dom.js';
import {request} from '/assets/client.js';

const terciles = ['below', 'normal', 'above'];
const tercileLabels = ['ниже нормы', 'около нормы', 'выше нормы'];
const segments = {seasonal_t2m: 'сезонный t2m', monthly_t2m: 'месячный t2m', seasonal_tp: 'сезонный tp', monthly_tp: 'месячный tp'};
const seasons = {DJF: 'зима (DJF)', MAM: 'весна (MAM)', JJA: 'лето (JJA)', SON: 'осень (SON)'};
const fixed = value => finite(value) ? value.toFixed(4) : '—';
const score = value => node('span', signed(value), finite(value) ? (value > 0 ? 'pos' : value < 0 ? 'neg' : '') : '');

function years(value, summaryId, tableId) {
    const block = record(value);
    byId(summaryId).textContent = `RPSS>0: ${text(block.positive_years)} из ${text(block.n_years)} лет · десятилетия ${signed(block.first_decade_rpss)} → ${signed(block.second_decade_rpss)} · лучший ${text(block.best?.year)} · худший ${text(block.worst?.year)}`;
    fillTable(byId(tableId), ['Год', 'n', 'RPSS', 'hit'], list(block.rows).map(row => [text(row.year), number(row.n, 0), score(row.rpss), fixed(row.hit)]));
}

export function renderValueReport(report) {
    const rep = record(report);
    byId('meta').textContent = `${number(rep.n_points, 0)} точек × ${text(rep.years_span)} · исходный аудит, не независимая проверка · ${number(rep.verifications, 0)} верификаций · ${text(rep.generated_at)}`;
    const seg = record(rep.segments);
    fillTable(byId('segtable'), ['Сегмент', 'n', 'RPS', 'RPS клим', 'RPSS', 'hit', 'Лучше климата', 'ECE', 'P10–P90'],
        Object.entries(segments).filter(([key]) => seg[key]).map(([key, label]) => {
            const row = seg[key];
            return [label + (key.endsWith('_tp') ? ' · навык не подтверждён' : ''), number(row.n, 0), fixed(row.rps), fixed(row.rps_clim), score(row.rpss), fixed(row.hit), fixed(row.win), fixed(row.ece), percent(row.coverage)];
        }));
    years(rep.years?.seasonal_t2m, 'y1sum', 'y1table');
    years(rep.years?.monthly_t2m, 'y2sum', 'y2table');
    const decomposition = record(rep.decomposition?.seasonal_t2m);
    const labels = {uniform: 'Равномерная климатология', greedy_warm: 'Жадный климат «всегда выше нормы»', blend: 'Бленд без NN и калибровки', product: 'Исходный продукт с NN и калибровкой'};
    fillTable(byId('dectable'), ['Прогноз', 'Средний RPS', 'RPSS', 'Выигрыш RPS'], Object.entries(labels).map(([key, label]) => {
        const row = record(decomposition[key]);
        return [label, fixed(row.rps), score(row.rpss), fixed(row.gain)];
    }));
    const calibration = record(seg.seasonal_t2m);
    fillTable(byId('caltable'), ['Терцель', 'Заявленная вероятность', 'Фактическая частота', 'Вклад в ECE'], terciles.map((key, i) => [tercileLabels[i], fixed(calibration.claimed?.[key]), fixed(calibration.freq?.[key]), fixed(calibration.ece_parts?.[key])]));
    byId('calsum').textContent = `ECE = ${fixed(calibration.ece)} · P10–P90 = ${percent(calibration.coverage)} · историческая оценка, не гарантия покрытия`;
    byId('seasbox').replaceChildren(...Object.entries(record(rep.seasons)).map(([mode, data]) => node('section', [
        node('h3', mode === 'seasonal' ? 'Сезонный режим' : mode === 'monthly' ? 'Месячный режим' : mode, 'season-heading'),
        table(['Сезон', 'n t2m', 'RPSS t2m', 'hit t2m', 'n tp', 'RPSS tp', 'hit tp'], Object.entries(seasons).map(([key, label]) => {
            const row = record(data?.[key]);
            return [label, number(row.t2m?.n, 0), score(row.t2m?.rpss), fixed(row.t2m?.hit), number(row.tp?.n, 0), score(row.tp?.rpss), fixed(row.tp?.hit)];
        }))
    ])));
    byId('notes').replaceChildren(...list(rep.notes).map(value => node('p', text(value), 'note literal')));
    byId('body').hidden = false;
}

async function load() {
    try {
        const payload = await request('/api/value');
        if (!payload.ok || !payload.report) throw new Error(payload.error || 'Исторический отчёт ещё не построен.');
        renderValueReport(payload.report);
    } catch (error) {
        byId('body').hidden = true;
        byId('errbox').hidden = false;
        byId('errtxt').textContent = error.message;
    }
}

load();
