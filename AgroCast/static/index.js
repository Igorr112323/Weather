import {byId, button, finite, link, list, node, number, record, table, text, uuid} from '/assets/dom.js';
import {logout, request, session} from '/assets/client.js';

let user;
let points = [];
let fieldRows = [];
let cropRows = [];
let editingField = null;
let editingCrop = null;
let map;
let mapLayer;
const writable = () => ['operator', 'admin'].includes(user?.role);
const cropInputs = {name: 'c_name', breeder: 'c_breeder', notes: 'c_notes', fao: 'c_fao', gdd: 'c_gdd', vp_days: 'c_vp', yield_t_ha: 'c_yield', frost_tol_c: 'c_ftol', frost_fatal_c: 'c_ffat'};

function clearField() {
    editingField = null;
    byId('field-form').reset();
}

function clearCrop() {
    editingCrop = null;
    byId('crop-form').reset();
}

async function removeResource(kind, row, label, reload, errorId) {
    if (!uuid(row.id) || !confirm('Удалить запись «' + text(label) + '»?')) return;
    try {
        await request('/api/' + kind + '/' + row.id, {method: 'DELETE'});
        await reload();
    } catch (error) {
        byId(errorId).textContent = error.message;
    }
}

function renderFields() {
    byId('fields').replaceChildren(fieldRows.length ? table(['Поле', 'Точка', 'Площадь, га', 'Действия'], fieldRows.map(row => {
        const data = record(row.data);
        const actions = writable() ? node('div', [
            button('Изменить', () => {
                editingField = row.id;
                byId('fname').value = text(data.name);
                byId('fpoint').value = data.point_id;
                byId('farea').value = data.area_ha;
                byId('fname').focus();
            }),
            button('Удалить', () => removeResource('fields', row, data.name, loadFields, 'ferr'))
        ], 'actions') : 'Только чтение';
        return [text(data.name), text(data.point_id), number(data.area_ha), actions];
    })) : node('p', 'Своих полей пока нет.'));
    renderMap();
}

async function loadFields() {
    fieldRows = list((await request('/api/fields')).fields).filter(row => uuid(row.id));
    renderFields();
}

async function loadCrops() {
    cropRows = list((await request('/api/crops')).crops).filter(row => uuid(row.id));
    byId('cropsbox').replaceChildren(cropRows.length ? table(['Сорт / селекционер', 'ФАО / САТ', 'Заметки', 'Действия'], cropRows.map(row => {
        const data = record(row.data);
        const actions = user.role === 'admin' ? node('div', [
            button('Изменить', () => {
                editingCrop = row.id;
                for (const [key, id] of Object.entries(cropInputs)) byId(id).value = data[key] ?? '';
                byId('c_name').focus();
            }),
            button('Удалить', () => removeResource('crops', row, data.name, loadCrops, 'cerr'))
        ], 'actions') : 'Только чтение';
        return [node('div', [node('strong', text(data.name)), node('p', text(data.breeder), 'literal')]), number(data.fao, 0) + ' / ' + number(data.gdd, 0), text(data.notes), actions];
    })) : node('p', 'Справочник организации пуст.'));
}

function renderMap() {
    if (!map) return;
    if (mapLayer) map.removeLayer(mapLayer);
    const layers = points.map(point => {
        const owned = fieldRows.filter(row => row.data?.point_id === point.id);
        const content = node('div', [node('strong', point.id), node('p', `${number(point.lat, 2)}°N · ${number(point.lon, 2)}°E`), owned.map(row => node('p', text(row.data.name), 'literal'))]);
        const marker = window.L.circleMarker([point.lat, point.lon], {radius: owned.length ? 9 : 6, color: '#bed793', fillColor: '#527642', fillOpacity: 0.9});
        marker.bindPopup(node('div', content));
        marker.bindTooltip(node('span', point.id));
        marker.on('click', () => { byId('fpoint').value = point.id; });
        return marker;
    });
    mapLayer = window.L.featureGroup(layers).addTo(map);
    if (layers.length) map.fitBounds(mapLayer.getBounds().pad(0.15));
    byId('map-status').textContent = points.length + ' проверочных точек. Это не карта разрешённых production-прогнозов.';
}

byId('field-form').addEventListener('submit', async event => {
    event.preventDefault();
    const submit = byId('fadd');
    submit.disabled = true;
    byId('ferr').textContent = '';
    try {
        const body = {name: byId('fname').value, point_id: byId('fpoint').value, area_ha: Number(byId('farea').value)};
        await request('/api/fields' + (editingField ? '/' + editingField : ''), {method: editingField ? 'PUT' : 'POST', body});
        clearField();
        await loadFields();
        byId('ferr').textContent = 'Поле сохранено.';
    } catch (error) {
        byId('ferr').textContent = error.message;
    } finally {
        submit.disabled = false;
    }
});

byId('crop-form').addEventListener('submit', async event => {
    event.preventDefault();
    const submit = byId('c_save');
    submit.disabled = true;
    byId('cerr').textContent = '';
    try {
        const body = {};
        for (const [key, id] of Object.entries(cropInputs)) {
            const value = byId(id).value;
            body[key] = ['name', 'breeder', 'notes'].includes(key) ? value : value === '' ? null : Number(value);
        }
        body.frost_tol_c ??= -2;
        body.frost_fatal_c ??= -3;
        await request('/api/crops' + (editingCrop ? '/' + editingCrop : ''), {method: editingCrop ? 'PUT' : 'POST', body});
        clearCrop();
        await loadCrops();
        byId('cerr').textContent = 'Сорт сохранён.';
    } catch (error) {
        byId('cerr').textContent = error.message;
    } finally {
        submit.disabled = false;
    }
});

byId('fclear').addEventListener('click', clearField);
byId('c_clear').addEventListener('click', clearCrop);
byId('logout').addEventListener('click', async () => {
    try { await logout(); } catch (error) { byId('identity-status').textContent = error.message; }
});

async function load() {
    try {
        user = await session();
        byId('identity-status').textContent = user.username + ' · роль: ' + user.role;
        byId('field-controls').disabled = !writable();
        byId('crop-controls').disabled = user.role !== 'admin';
        const capabilities = await request('/api/capabilities');
        points = list(capabilities.regions?.find(region => region.id === 'krai')?.inspection_points).filter(point => typeof point.id === 'string' && finite(point.lat) && finite(point.lon));
        byId('fpoint').replaceChildren(...points.map(point => {
            const option = node('option', point.id);
            option.value = point.id;
            return option;
        }));
        try {
            map = window.L.map('map', {attributionControl: false, scrollWheelZoom: false, maxZoom: 12});
        } catch {
            byId('map-status').textContent = 'Схема карты недоступна. Точки остаются в списке поля.';
        }
        await Promise.all([loadFields(), loadCrops()]);
        const [jobs, subscriptions] = await Promise.all([request('/api/jobs'), request('/api/subscriptions')]);
        const rows = list(jobs.jobs).filter(row => uuid(row.id));
        byId('jobs').replaceChildren(rows.length ? table(['Запись', 'Состояние'], rows.map(row => [link(text(row.data?.title || row.data?.report?.title || row.id), '/report.html?' + new URLSearchParams({job: row.id})), text(row.status)])) : node('p', 'Сохранённых отчётов пока нет.'));
        const drafts = list(subscriptions.subscriptions);
        byId('subscriptions').replaceChildren(drafts.length ? table(['Название', 'Начальный месяц', 'Состояние'], drafts.map(row => [text(row.data?.name), number(row.data?.start_month, 0), 'Неактивна'])) : node('p', 'Своих настроек подписок пока нет.'));
        byId('load-status').textContent = 'Данные загружены. Изменения проверяются сервером по роли и владельцу.';
    } catch (error) {
        byId('load-status').textContent = error.message;
    }
}

load();
