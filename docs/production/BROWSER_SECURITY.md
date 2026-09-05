# T03 · Безопасный DOM, CSP и браузерные проверки

Дата: **2026-09-05**. Политика DOM: **`dom-csp-v1`**; текущая политика API после T04 — **`closed-pilot-v4`**. Изменения HTTP/кэша: [API_CACHE.md](API_CACHE.md).

[Прогресс](PROGRESS.md) · [Scope](PILOT.md) · [Identity и запуск](IDENTITY.md) · [План](PLAN.md)

## Изменения

Убраны строковые сборки HTML из трёх исходных страниц `index.html`, `report.html`, `value.html`. Скрипты и стили вынесены в локальные файлы. Нет inline script/style, HTML-обработчиков событий и вставки пользовательских данных через `innerHTML`, `outerHTML`, `insertAdjacentHTML`, HTML parser или string evaluation.

Общий `static/dom.js` создаёт элементы из ограниченного набора тегов и добавляет данные текстовыми узлами. Имена поля/сорта, селекционер, заметки, заголовки, журналы и ошибки не становятся разметкой или атрибутами. Переносы строк и Unicode сохраняются. Кнопки получают `addEventListener`, а адреса записей формируются только из валидного UUID и относительного same-origin пути.

Параметры CSS диаграмм вычисляются только из конечных чисел; некорректные вероятности не превращаются в CSS и помечаются как некорректные. В JSON-инспекторе ограничены вложенность и число отображаемых элементов, с явной пометкой и доступом владельца к исходному JSON. Это не новая научная валидация модели.

## Что доступно в браузере

- `/workspace` и alias `/index.html`: собственные поля, справочник своей организации, ссылки на свои сохранённые отчёты и чтение неактивных настроек подписок.
- Создание/редактирование/удаление полей и сортов использует уже существующие права T02, UUID и CSRF. У reader элементы записи недоступны; API по-прежнему самостоятельно проверяет роль и владельца.
- `/report.html?job=<uuid>`: только существующая SQL-запись своего владельца. Сервер проверяет ownership **до выдачи HTML**, затем API повторно проверяет доступ к данным.
- Отчёт — исследовательский архив: сохранённые вероятности/контекст, снимки имён полей и сорта, исходные разделы и безопасный просмотр структуры. Старые советы/денежные сценарии представлены как неперепроверенные архивные данные, не как разрешённые предписания.
- `/value.html`: прежние таблицы исторического паспорта и ограничения аудита, но безопасный DOM.
- Новые прогнозы, hindcast, refresh, создание jobs и активация подписок **не включены**. На карте нет произвольного выбора России, внешней подложки или нового расчёта.

Старые поля из localStorage не подмешиваются к аккаунту или отчёту. Редактирование текущего поля/сорта не меняет снимок в сохранённой записи. Новая верстка не выдаётся за завершение всего UI/SLO из T21/T22.

## Content Security Policy

CSP задаётся HTTP-заголовком, не report-only и не только meta-тегом:

```text
default-src 'none';
script-src 'self';
script-src-attr 'none';
style-src 'self';
style-src-attr 'none';
img-src 'self' data:;
font-src 'self';
connect-src 'self';
object-src 'none';
base-uri 'none';
form-action 'self';
frame-src 'none';
frame-ancestors 'self' https://arena.ai https://*.arena.ai;
worker-src 'none';
manifest-src 'self';
upgrade-insecure-requests
```

Нет `unsafe-inline`, `unsafe-eval`, wildcard для скриптов или внешнего CDN. `frame-ancestors` разрешает self и доверенный интерфейс Arena, а не произвольные `*.e2b.app`; несовместимый `X-Frame-Options: DENY` не добавлялся. Действующая identity требует HTTPS и точного Origin; во встроенном preview сторонние cookies могут требовать открытия отдельной вкладки.

Политика добавляется к HTML, assets, JSON, 401/403, redirects и обработанному внешним exception handler ответу 500. Дополнительно остаются `nosniff`, `no-referrer`, `no-store`, `Vary: Cookie`, запрет индексации; Permissions-Policy отключает camera/microphone/geolocation.

CSSOM-записи доверенного JS (`element.style.width`/позиционирование Leaflet) не равны вставке произвольного style-атрибута. Их нормальная работа проверена браузером без CSP violations.

**Trusted Types enforcement не заявляется.** Leaflet 1.9.4 содержит внутренние HTML sinks для своих контролов. Не добавлен обходной default TrustedHTML policy: наш код передаёт в popup/tooltip только построенные DOM-узлы, а CSP и тесты проверяют этот путь. Любое расширение HTML API библиотеки требует отдельного пересмотра.

## Поставка Leaflet

Leaflet **1.9.4** поставляется из локальных assets. Нет загрузки кода со стороннего CDN и нет запросов OSM tiles. Карта явно подписана как схема P01–P28, не географическая подложка/поле нового прогноза.

- Источник и build tools закреплены в `AgroCast/package-lock.json`.
- `web/vendor.mjs` воспроизводимо собирает поставку через esbuild 0.25.10.
- `static/vendor/leaflet/manifest.json` содержит npm integrity, SHA-256 готовых файлов и SRI.
- HTML использует SRI для JS/CSS Leaflet.
- Лицензия BSD-2-Clause сохранена рядом в `LICENSE.txt`, сведения о поставке — в `THIRD_PARTY_NOTICES.md`. Комментарии/source-map references удалены из производного JS/CSS, не из лицензионного документа.
- Публичен только явный список assets в `serve/browser_policy.py`, не весь каталог `static` и не произвольный путь.

```bash
cd AgroCast
npm ci --ignore-scripts
npm run check:vendor
npm run check:dom
```

`check:dom` разбирает AST first-party JS: запрещает HTML/string-execution sinks, опасные атрибуты, чтение пользовательского состояния из browser storage и строковый контент popup/tooltip. Vendor исключён из этого AST-запрета, но проверяется побайтово по закреплённому источнику и покрывается браузерным тестом.

## Браузерные сценарии

Playwright работает с настоящим HTTPS Uvicorn-приложением, временным сертификатом и отдельной БД. Это не замена страниц тестовой разметкой и не фиктивный auth: вход, cookie, CSRF и CRUD проходят через реальный API. SQLite используется как явно инъецированный test engine; runtime fallback с PostgreSQL не добавлен.

Десять сценариев проверяют:

1. Сохранение, редактирование по UUID, reload и удаление поля/сорта с кавычками, тегами и Unicode.
2. Буквальное отображение имени/селекционера/заметок и Leaflet popup без созданных payload-элементов, alert или побочных запросов.
3. Сохранение тестового отчёта со снимками действительно записанных полей/сорта и повторное открытие через защищённый HTML/API; без смешивания с текущими полями/localStorage.
4. Исторические метаданные и заметки паспорта.
5. Сохранённую ошибку failed job и ошибку API как обычный текст.
6. Отдельные canary-вставки, которые браузер блокирует CSP: inline script, inline handler, style-атрибут и внешний fetch.
7. Права reader, отказ прямому API-запуску расчёта, недоступность HttpOnly cookie для JS.
8. Отказ чужому владельцу ещё на запросе HTML отчёта.
9. Выход, отзыв cookie и невозможность открыть отчёт через history после выхода.
10. Сохранение предупреждения при print media.

Сценарии сохранённых строк повторяются в двух контекстах: с CSP и с `bypass_csp=True` **только в тестовом браузере**. Поэтому отсутствие XSS не маскируется одним заголовком: DOM остаётся текстовым даже без него. В приложении переключателя отключения CSP нет.

Отчёт записывает доверенная test fixture в SQL, поскольку создание новых forecasts/jobs по HTTP намеренно запрещено. В одном негативном сценарии Playwright подставляет ошибочный JSON-ответ, чтобы проверить отображение недоверенного текста ошибки; он не подменяет успешное сохранение, login или ownership.

## Запуск и CI

```bash
.venv/bin/python -m pip install -r AgroCast/requirements-browser.txt
.venv/bin/python -m playwright install --with-deps chromium
cd AgroCast
AGROCAST_BROWSER_TESTS=1 AGROCAST_BROWSER_ENGINE=chromium ../.venv/bin/python -m pytest tests/browser -q
```

Обычный быстрый набор: `python -m pytest tests -q -m 'not browser'`. Browser tests не засчитываются как выполненные, если флаг не включён. CI имеет отдельные Chromium/Firefox jobs, проверку vendored bytes/DOM AST и артефакты ошибок с семидневным хранением. Screenshot маскирует поля password; полный Playwright network trace с телами login не сохраняется.

В sandbox CDN официальных браузеров Playwright не был доступен по TLS. Локально тесты выполнены через Playwright **1.62.0** на **Chromium 149.0.7827.0**, установленном отдельно из `@sparticuz/chromium@149.0.0`; `AGROCAST_BROWSER_EXECUTABLE` указывал на тестовый бинарник. Это dev-only инструмент, не зависимость production-приложения. Никаких TLS-проверок package download не отключалось. Self-signed TLS игнорируется только в браузерном контексте теста.

Точные результаты: [PROGRESS.md](PROGRESS.md). **Firefox локально не запущен, remote GitHub Actions не запускались.** Production Caddy/TLS, внешний pentest, полная доступность и release gates остаются непроверенными.
