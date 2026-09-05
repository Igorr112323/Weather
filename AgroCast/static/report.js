import {byId, detailTree, finite, link, list, node, number, panel, percent, record, table, text, uuid} from '/assets/dom.js';
import {request} from '/assets/client.js';

const words = ['ниже нормы', 'около нормы', 'выше нормы'];

function probabilities(value) {
    const values = Array.isArray(value) ? value : [value?.below, value?.normal, value?.above];
    if (values.length !== 3 || !values.every(v => finite(v) && v >= 0 && v <= 1) || Math.abs(values.reduce((a, b) => a + b, 0) - 1) > 0.001) {
        return node('p', 'Сохранённые вероятности отсутствуют или некорректны.', 'hint');
    }
    const bar = node('div', values.map((value, index) => {
        const segment = node('span', percent(value), 'c' + index);
        segment.style.width = String(value * 100) + '%';
        segment.title = words[index] + ': ' + percent(value);
        return segment;
    }), 'tbar');
    return node('div', [bar, node('p', values.map((value, index) => words[index] + ': ' + percent(value)).join(' · '), 'hint')]);
}

function forecastVariable(value, name, unit) {
    const data = record(value);
    const quantiles = record(unit === '°C' ? data.quantiles_c : data.quantiles_mm);
    const normal = unit === '°C' ? data.normal_c : data.normal_mm;
    const confidence = typeof data.confidence === 'object' ? text(data.confidence?.label ?? data.confidence?.level ?? data.confidence) : text(data.confidence);
    return node('section', [node('h3', name), probabilities(data.tercile_probs), table(['Сохранённый показатель', 'Значение'], [
        ['P50', number(quantiles.p50) + ' ' + unit],
        ['P10–P90', number(quantiles.p10) + ' … ' + number(quantiles.p90) + ' ' + unit],
        ['Норма из отчёта', number(normal) + ' ' + unit],
        ['Исходная оценка уверенности, не допуск', confidence],
        ['Годы-аналоги', list(data.analog_years).map(row => text(row.year)).join(', ') || '—']
    ])]);
}

function hindcastVariable(value, name) {
    const data = record(value);
    if (!Object.keys(data).length) return node('section', node('h3', name));
    return node('section', [node('h3', name), probabilities(data.probs), table(['Сохранённый показатель', 'Значение'], [
        ['Исходное сравнение', data.hit === true ? 'попадание' : data.hit === false ? 'промах' : '—'],
        ['Факт: терцель', Number.isInteger(data.obs) && data.obs >= 0 && data.obs <= 2 ? words[data.obs] : '—'],
        ['P50 / факт / норма', [data.p50, data.fact, data.norm].map(value => number(value)).join(' / ')],
        ['Единицы исходных данных', text(data.unit)]
    ])]);
}

function archiveSection(title, value) {
    const section = node('details', [node('summary', title), detailTree(value)], 'block');
    return section;
}

export function renderReport(job, validation) {
    const data = record(job.data);
    const report = record(data.report);
    const title = report.title || data.title || 'Сохранённый отчёт AgroCast';
    byId('report-title').textContent = text(title);
    document.title = 'AgroCast · ' + text(title);
    byId('report-meta').textContent = `Запись ${text(job.id)} · ${text(job.status)} · период ${text(report.start)} · данные по ${text(report.issue_data_through)}`;
    const root = byId('app');
    root.replaceChildren();
    if (job.status === 'failed') {
        byId('report-error').hidden = false;
        byId('report-error').textContent = text(data.error || 'Сохранённая задача завершилась ошибкой.');
    }
    if (job.status !== 'succeeded') {
        root.append(archiveSection('Сохранённое состояние и журнал', data));
        byId('report-status').textContent = 'Новый расчёт или polling не запускается.';
        return;
    }
    root.append(panel('Контекст исходного расчёта', table(['Параметр', 'Сохранённое значение'], [
        ['Тип / режим', text(report.kind) + ' / ' + text(report.mode)],
        ['Точка', number(report.lat, 3) + '°N · ' + number(report.lon, 3) + '°E'],
        ['Период', text(report.start)],
        ['Статус проверки', text(validation?.status || 'unverified')]
    ])));
    const fields = list(report.fields_snapshot || report.fields);
    if (fields.length) root.append(panel('Поля в сохранённом отчёте', table(['Поле', 'Точка / культура', 'Площадь, га'], fields.map(row => {
        const field = record(row.data || row);
        return [text(field.name), text(field.point_id || field.crop), number(field.area_ha ?? field.area)];
    }))));
    const crop = record(report.crop_snapshot || report.agro?.insight?.frost?.crop);
    if (Object.keys(crop).length) root.append(panel('Сорт в сохранённом отчёте', table(['Параметр', 'Значение'], [
        ['Название', text(crop.name)], ['Селекционер', text(crop.breeder)], ['Заметки', text(crop.notes)], ['ФАО', number(crop.fao, 0)]
    ])));
    if (report.kind === 'hindcast') {
        root.append(panel('Итог исходной проверки, не независимое подтверждение', detailTree(report.summary || {})));
        for (const item of list(report.items)) {
            root.append(panel('Исходная цель: ' + text(item.year) + ' · месяц ' + text(item.target_month), node('div', [hindcastVariable(item.t2m, 'Температура'), hindcastVariable(item.tp, 'Осадки')], 'row2')));
        }
    } else {
        for (const item of list(report.seasons || report.months)) {
            const title = list(item.months).map(text).join(' – ') || text(item.month) + ' / ' + text(item.year);
            root.append(panel('Исходный период: ' + title + ' · lead ' + text(item.lead), node('div', [
                item.t2m ? forecastVariable(item.t2m, 'Температура', '°C') : undefined,
                item.tp ? forecastVariable(item.tp, 'Осадки', 'мм') : undefined
            ], 'row2')));
            if (item.advice) root.append(archiveSection('Исходные советы периода · не допущены к применению', item.advice));
        }
    }
    const agro = record(report.agro);
    const sections = {
        what_to_do: 'Исходные действия · не агрорекомендация', passport: 'Исходный паспорт навыка', trust_ledger: 'Исходный реестр оценок',
        season_indices: 'Агроиндексы исходного периода', insight: 'Декады, вода, САТ и риски · неперепроверенные данные',
        phenology: 'Исходный фено-календарь', drivers: 'Драйверы исходного расчёта', decisions: 'Исходные сценарные решения',
        econ: 'Исходные денежные сценарии · не гарантия дохода', analogs_facts: 'Исходные сведения о годах-аналогах'
    };
    for (const [key, value] of Object.entries(agro)) root.append(archiveSection(Object.hasOwn(sections, key) ? sections[key] : key, value));
    root.append(archiveSection('Полная сохранённая структура · безопасный просмотр', data));
    if (uuid(job.id)) root.append(link('Открыть исходную JSON-запись своего отчёта', '/api/jobs/' + job.id));
    byId('report-status').textContent = 'Сохранённая запись загружена. Текущие поля браузера в неё не подмешиваются.';
}

byId('print').addEventListener('click', () => window.print());

async function load() {
    try {
        const id = new URLSearchParams(location.search).get('job');
        if (!uuid(id)) throw new Error('Укажите корректный UUID сохранённой записи.');
        const response = await request('/api/jobs/' + id);
        renderReport(response.job, response.validation);
    } catch (error) {
        byId('report-error').hidden = false;
        byId('report-error').textContent = error.message;
        byId('report-status').textContent = 'Отчёт недоступен.';
    }
}

load();
