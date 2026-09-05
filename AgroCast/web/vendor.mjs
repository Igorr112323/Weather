import {createHash} from 'node:crypto';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {resolve, dirname} from 'node:path';
import {transform} from 'esbuild';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const upstream = resolve(root, 'node_modules/leaflet');
const destination = resolve(root, 'static/vendor/leaflet');
const checking = process.argv.includes('--check');
const packageInfo = JSON.parse(await readFile(resolve(upstream, 'package.json'), 'utf8'));
if (packageInfo.version !== '1.9.4') throw new Error('Unexpected Leaflet version');
const lock = JSON.parse(await readFile(resolve(root, 'package-lock.json'), 'utf8'));
const files = {};
for (const [name, loader] of [['leaflet.js', 'js'], ['leaflet.css', 'css']]) {
    const source = await readFile(resolve(upstream, 'dist', name), 'utf8');
    files[name] = Buffer.from((await transform(source, {loader, minify: true, legalComments: 'none', charset: 'utf8', target: 'es2020'})).code);
}
for (const name of ['layers.png', 'layers-2x.png', 'marker-icon.png', 'marker-icon-2x.png', 'marker-shadow.png']) {
    files['images/' + name] = await readFile(resolve(upstream, 'dist/images', name));
}
files['LICENSE.txt'] = await readFile(resolve(upstream, 'LICENSE'));
const manifest = {
    package: 'leaflet', version: packageInfo.version,
    source: lock.packages['node_modules/leaflet'].resolved,
    integrity: lock.packages['node_modules/leaflet'].integrity,
    builder: 'esbuild@0.25.10',
    files: Object.fromEntries(Object.entries(files).map(([name, bytes]) => [name, {
        sha256: createHash('sha256').update(bytes).digest('hex'),
        sri: 'sha384-' + createHash('sha384').update(bytes).digest('base64')
    }]))
};
files['manifest.json'] = Buffer.from(JSON.stringify(manifest, null, 2) + '\n');
for (const [name, bytes] of Object.entries(files)) {
    const target = resolve(destination, name);
    if (checking) {
        if (!(await readFile(target)).equals(bytes)) throw new Error('Vendor drift: ' + name);
    } else {
        await mkdir(dirname(target), {recursive: true});
        await writeFile(target, bytes);
    }
}
console.log(checking ? 'Leaflet assets match the locked source' : 'Leaflet assets generated; license preserved separately');
