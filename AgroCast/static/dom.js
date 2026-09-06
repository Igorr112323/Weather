export const byId = id => document.getElementById(id);
export const list = value => Array.isArray(value) ? value : [];
export const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
export const finite = value => typeof value === 'number' && Number.isFinite(value);
export const text = value => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
export const number = (value, digits = 1) => finite(value) ? value.toLocaleString('ru-RU', {maximumFractionDigits: digits}) : '—';
export const percent = value => finite(value) ? number(100 * value, 1) + '%' : '—';
export const signed = (value, digits = 4) => finite(value) ? (value > 0 ? '+' : '') + value.toFixed(digits) : '—';
export const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

function append(parent, value) {
    if (Array.isArray(value)) {
        value.forEach(child => append(parent, child));
    } else if (value instanceof Node) {
        parent.append(value);
    } else if (value !== undefined) {
        parent.append(document.createTextNode(text(value)));
    }
}

const tags = new Set(['a', 'b', 'button', 'caption', 'dd', 'details', 'div', 'dl', 'dt', 'h2', 'h3', 'i', 'option', 'p', 'section', 'span', 'strong', 'summary', 'table', 'tbody', 'td', 'th', 'thead', 'tr']);

export function node(tag, children = [], className = '') {
    if (!tags.has(tag)) throw new Error('Unsupported DOM element');
    const result = document.createElement(tag);
    if (className) result.className = className;
    append(result, children);
    return result;
}

export function button(label, handler, className = '') {
    const result = node('button', label, className);
    result.type = 'button';
    result.addEventListener('click', handler);
    return result;
}

export function link(label, path, className = '') {
    const target = new URL(path, location.origin);
    if (target.origin !== location.origin || !path.startsWith('/') || path.startsWith('//')) throw new Error('Only local links are allowed');
    const result = node('a', label, className);
    result.href = target.pathname + target.search + target.hash;
    return result;
}

export function table(headings, rows, caption = '') {
    const result = node('table');
    if (caption) result.append(node('caption', caption));
    const headers = headings.map(value => {
        const header = node('th', value);
        header.scope = 'col';
        return header;
    });
    result.append(node('thead', node('tr', headers)));
    result.append(node('tbody', rows.map(values => node('tr', values.map(value => node('td', value))))));
    return result;
}

export function fillTable(target, headings, rows) {
    target.replaceChildren(...table(headings, rows).childNodes);
}

export function panel(title, children, className = 'block') {
    return node('section', [node('h2', title), children], className);
}

export function detailTree(value, budget = {remaining: 1500}, depth = 0) {
    if (--budget.remaining < 0 || depth > 8) return node('p', 'Показан предел вложенности/объёма. Полная запись сохранена в API.', 'hint');
    if (value === null || typeof value !== 'object') return node('span', text(value), 'literal');
    const entries = Object.entries(value).slice(0, 100);
    if (!entries.length) return node('span', Array.isArray(value) ? '[]' : '{}', 'literal');
    const container = node('dl', [], 'details-grid');
    for (const [key, child] of entries) {
        if (budget.remaining <= 0) break;
        container.append(node('dt', key), node('dd', detailTree(child, budget, depth + 1)));
    }
    if (Object.keys(value).length > entries.length || budget.remaining <= 0) container.append(node('dt', 'Предел просмотра'), node('dd', 'Полное содержимое доступно в сохранённой JSON-записи.'));
    return container;
}
