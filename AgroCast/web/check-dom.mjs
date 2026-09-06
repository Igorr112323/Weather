import {parse} from 'acorn';
import {readdir, readFile} from 'node:fs/promises';
import {resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'static');
const forbidden = new Set(['innerHTML', 'outerHTML', 'insertAdjacentHTML', 'write', 'writeln', 'createContextualFragment', 'parseFromString', 'cssText', 'srcdoc']);
let count = 0;
for (const file of await readdir(root)) {
    if (!file.endsWith('.js')) continue;
    const source = await readFile(resolve(root, file), 'utf8');
    const comments = [];
    const tree = parse(source, {ecmaVersion: 'latest', sourceType: 'module', onComment: comments});
    if (comments.length) throw new Error(file + ': comments are not permitted');
    function visit(value) {
        if (!value || typeof value !== 'object') return;
        if (value.type === 'MemberExpression') {
            const name = value.computed ? value.property.value : value.property.name;
            if (forbidden.has(name)) throw new Error(file + ': forbidden HTML/style sink ' + name);
            if (['localStorage', 'sessionStorage'].includes(name)) throw new Error(file + ': browser storage must not supply user identity/data');
        }
        if (value.type === 'Identifier' && ['eval', 'Function', 'DOMParser', 'localStorage', 'sessionStorage'].includes(value.name)) throw new Error(file + ': forbidden API ' + value.name);
        if (value.type === 'CallExpression' && ['setTimeout', 'setInterval'].includes(value.callee.name) && typeof value.arguments[0]?.value === 'string') throw new Error(file + ': string timer');
        const contentCalls = ['bindPopup', 'bindTooltip', 'setPopupContent', 'setTooltipContent', 'setContent'];
        if (value.type === 'CallExpression' && contentCalls.includes(value.callee.property?.name)) {
            const input = value.arguments[0];
            if (input?.type !== 'CallExpression' || input.callee.name !== 'node') throw new Error(file + ': Leaflet content must be an explicit DOM node');
        }
        if (value.type === 'Property' && (value.key.name === 'html' || value.key.value === 'html')) {
            if (value.value.type !== 'CallExpression' || value.value.callee.name !== 'node') throw new Error(file + ': HTML options require DOM nodes');
        }
        if (value.type === 'CallExpression' && value.callee.property?.name === 'setAttribute') {
            const name = value.arguments[0]?.value;
            if (typeof name !== 'string' || /^on/i.test(name) || ['style', 'srcdoc'].includes(name)) throw new Error(file + ': unsafe attribute writer');
        }
        for (const child of Object.values(value)) if (Array.isArray(child)) child.forEach(visit); else if (child && typeof child === 'object') visit(child);
    }
    visit(tree);
    count += 1;
}
console.log(count + ' first-party scripts: no HTML parsers, string execution, inline-style sinks or browser-storage data');
