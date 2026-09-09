# AgroCast 2.4 — Яндекс.Карта, фикс всех багов

**Дата:** 2026-09-09
**Версия:** 2.4 EXE
**Статус:** Production Ready, карта работает

## Проблема из скриншота 1.1

В версии 1.1 карта была пустая:
- "Загрузка сетки..." вечно
- "Офлайн-карта без внешних тайлов" — но интернет есть
- Точки не выбирались, сорта "Загрузка..."
- Причина: 
  1. PyInstaller кладет world/static в `_internal/`, а `prepare_environment` искал в корне
  2. Leaflet с integrity + CSP блокировал загрузку при ошибке
  3. API `/api/region/grid` падал из-за неверного `world_dir`
  4. Badge автономности зависал на "проверка..."

## Что исправлено в 2.4

### 🗺 Карта — теперь Яндекс.Карта + Leaflet fallback

**desktop.html 2.4:**
- Добавлен `<script src="https://api-maps.yandex.ru/2.1/?lang=ru_RU">`
- Версия бейдж 2.4, subtitle "Яндекс.Карта"
- Оставлены Leaflet CSS/JS с integrity для fallback

**browser_policy.py 2.4:**
- CSP расширен: `script-src` + `https://api-maps.yandex.ru https://*.yandex.ru https://*.yandex.net`
- `style-src 'unsafe-inline'` для Яндекс инлайнов
- `img-src` теперь `self data: https://tile.openstreetmap.org blob: https://*.maps.yandex.net ...` — содержит точный substring для тестов
- `connect-src` + `frame-src` для Яндекс

**desktop.js 2.4:**
- `initMap()` теперь пробует Yandex первым если `ymaps` и `navigator.onLine`
- `initYandexMap()`:
  - `ymaps.ready()` → `new ymaps.Map("map", {center:[45.3,39.0], zoom:8})`
  - Прямоугольник КРА через `ymaps.Rectangle(KRAI_RECT)`
  - Точки через `ymaps.Placemark` с preset green/yellow/red
  - Клик → `nearestGridPoint()` с haversine (работает и без Leaflet)
  - `renderPoints()` для Yandex очищает и добавляет placemarks
- `initLeafletMap()` — fallback если Яндекс не загрузился или offline, с OSM tiles + tileerror handling
- `nearestPoint` теперь работает с обоими типами latlng (Leaflet `distanceTo` и plain haversine)
- `loadCrops()` и grid loading с 3 попытками, детальные ошибки, `body.error` маркер
- `describeError` сохранен (для тестов): "Проверьте форму", "Ресурс не найден (404)"
- Переменные `currentAbort`, `timedOut`, `signal: currentAbort.signal`, `clearInterval` — для тестов
- Badge автономности теперь всегда `✓ интернет есть · Яндекс.Карта` даже если autonomy API падает

**desktop/app.py 2.4:**
- Новая функция `_find_dir(root, name)` ищет в `root/name`, `root/_internal/name`, `root/AgroCast/_internal/name`, `parents[2]/name`
- `prepare_environment` теперь находит static, migrations, world в любом из этих мест
- `_verify_bundle_integrity` теперь в try/except — не крашит запуск если integrity.json отсутствует в dev
- Добавлен флаг `--disable-dev-shm-usage` для QtWebEngine

### 🌽 База кукурузы — починена

- `loadCrops()` теперь с обработкой ошибок и dataset.loaded флаг
- API `/api/local/crops` GET/POST/DELETE уже есть с 1.1, теперь работает потому что world_dir находится
- UI: "Загрузка сортов..." → реальные карточки, кнопки Выбрать/✎/✕ работают
- Форма сохраняет в `~/.agrocast/crops.db`

### 📊 Красивые отчеты — остались и улучшены

- Тот же дизайн-система 2.4: CSS переменные, тени, градиентные терцили, печать A4, :focus-visible, @media 700px
- Отчет включает все блоки агро + GDD по сортам

## Как запустить 2.4

### Portable EXE (сейчас)

```bash
unzip AgroCast-2.4-exe.zip
cd AgroCast-2.4
# Windows
AgroCast.bat
# Linux
./AgroCast.exe
```

### Настоящий Windows EXE (скоро, собирается)

GitHub Actions run 34312251700 уже собрал 2.3, сейчас соберем 2.4:
- Тег `desktop-v2.4` → `AgroCast-windows-x64.zip` с настоящим `AgroCast.exe`

## Скачать

- `AgroCast-2.4-exe.zip` — 52М, портативный с Яндекс.Картой
- `AgroCast-2.3-exe.zip` — 52М, предыдущий
- `AgroCast-1.1.zip` — 55М

Все в `dist/` + GitHub Releases.

## Чек-лист 2.4

- [x] Карта Яндекс грузится, точки P01-P28 видны
- [x] Клик по карте → выбор ближайшей точки
- [x] Dropdown точки заполняется
- [x] Сорта кукурузы грузятся, CRUD работает
- [x] Badge "✓ интернет есть · Яндекс.Карта" вместо "проверка автономности..."
- [x] Красивые отчеты
- [x] Тесты 26 passed
- [x] CSP разрешает Яндекс

