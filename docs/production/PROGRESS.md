# Прогресс реализации production-плана

[План](PLAN.md) · [Scope пилота](PILOT.md) · [Identity и запуск](IDENTITY.md) · [DOM/CSP](BROWSER_SECURITY.md) · [HTTP/кэш](API_CACHE.md) · [Исходный аудит](AUDIT.md) · [Release gates](RELEASE_CHECKLIST.md)

## Состояние на 2026-09-05

**T01–T04 реализованы и локально проверены. T05–T24 ещё не завершены. Production-релиз не разрешён.**

Работа идёт по одному пункту с проверкой результата. Минимальная SQL-схема из зависимости T05 подготовлена для T02; это не выполнение полного T05, очереди T06, readiness T07 или всех CI gates T17.

| Пункт | Статус | Результат |
|---|---|---|
| T01 · Scope закрытого пилота | Реализован; ограничения сохранены | Новые прогнозы/агрорекомендации запрещены API, исторические результаты помечены неперепроверенными |
| T02 · Identity / роли / ownership | Реализован; проверен на PostgreSQL | Личные аккаунты, server sessions, CSRF, reader/operator/admin, owner/organization и миграция |
| T03 · DOM / CSP | Реализован; Chromium-проверки пройдены | Текстовые DOM-узлы, enforced CSP, локальный Leaflet, реальные HTTPS browser tests |
| T04 · HTTP / cache | Реализован; локальные проверки пройдены | Строгие модели/ошибки, crop revision, полный ключ и атомарный региональный cache |
| T05 · Settings / persistent state | Следующий; не завершён | Минимальная identity-схема не заменяет полный перенос runtime state и настройки |
| T06–T24 | Ожидают исполнения | Подготовительные изменения не закрывают соответствующие задачи |

## T04 · Реализованный результат

- Общие Pydantic enums и строгие численные/календарные ограничения, проверка mode/season_len/полного периода, корректная пара variety UUID/revision и support matrix перед возможным вычислением.
- Operator/admin получают 422 за неправильное тело prepare/refresh; корректный запрос остаётся 403 по пилоту. Reader — 403 раньше валидации, аноним — 401. В HTTP больше нет старых ветвей записи в `JOBS`/старта потоков, регионального TTL-only short circuit и тихого fallback режима.
- Единая ошибка `code/error/detail`, strict JSON, отказ duplicate/unknown query и безопасный 500 при response validation. Активные account/resource/history endpoints снабжены response models. Исторический opaque JSON не объявлен научно валидированным.
- Авторизованный `/api/contracts` отдаёт OpenAPI с auth/CSRF/Origin и отметкой закрытого вычислительного допуска. Формат 202/Location для будущего принятия заданий реализован и проверен отдельно; настоящая очередь/приём не имитируются.
- `0002_crop_revision` добавляет атомарный счётчик версии сорта. Existing UUID/owner/org/JSON/timestamps сохранены при миграции, в том числе на настоящем PostgreSQL. Сорт разрешается по UUID, ожидаемой revision и организации.
- Подписки сохраняют month/horizon/mode/season_len/variables и сорт/версию. Чужой сорт — 404, изменённая revision — 409; рассылка и новые вычисления по-прежнему отключены.
- Полный canonical result key включает цель, start/horizon/mode/season_len/kind/variables, owner/org namespace, сорт+revision+content hash, data/model/application releases и настройки. Общая модель покрывает точку/регион; реально подключён существующий региональный builder/scheduler.
- Результат публикуется атомарно вместе с identity/checksum/временем. Неподходящий, повреждённый, частичный или устаревший файл даёт miss; mtime не заменяет параметры. Проверяются адреса/месяцы результата ячеек перед публикацией.
- Старые unversioned региональные файлы остаются архивом, не hit нового cache. Без явных release IDs нет fallback на «latest»/mtime. Immutable bundles/provenance остаются T14/T15, readiness — T07.

### Проверки T04

| Проверка | Результат |
|---|---|
| Весь небраузерный набор с покрытием новых модулей | **443 passed, 2 skipped, 10 deselected**, 38 warnings, 171.35 с |
| PostgreSQL: контракт/миграция, identity, роли, владение, CSP HTTP | **190 passed**, без пропусков, 2 warnings, 76.50 с |
| Два PostgreSQL-only случая из быстрого набора | Выполнены во втором прогоне: concurrent last-admin и реальная factory из env |
| Browser regression Chromium по HTTPS | **10 passed**, 2 warnings, 19.95 с |
| Покрытие core contracts/jsoncodec, API errors/responses, result cache | **96%**, совокупное line/branch coverage выбранных модулей, не всего репозитория |
| Новый кэш | 31 focused case: март/октябрь, параметры, releases/settings/owner, exact coordinates, corruption/stale/atomicity/concurrent readers, builder и scheduler |
| Миграция | Проверено обновление 0001 → 0002 с существующими данными, повторное применение и реальное scoped variety snapshot |
| Научные bundles | `AgroCast/world` не изменён; mini5/переаудит/перепубликация моделей не выполнялись. Обычные прогнозные smoke tests входят в pytest |

Полные наборы выполнялись параллельно на 2 vCPU, поэтому длительности не являются SLO. PostgreSQL — прежняя изолированная тестовая сборка 16.2 с Unix socket; browser — Chromium 149.0.7827.0 / Playwright 1.62.0. Production PostgreSQL 17 image, Docker/Caddy rollout, remote CI, Firefox и внешний pentest не проверялись. Existing warnings не скрыты.

Результаты cache tests получены тестовыми вычислителями, без NOAA/новых региональных forecast. 202 проверен как транспортный контракт, а не как существующая durable queue. Legacy offline forecaster/SQLite registry не становятся production-safe из-за этих изменений; их полный перенос/валидация остаются соответствующим следующим задачам. [Контракт, миграция и ограничения releases](API_CACHE.md).

## История T03 · Реализованный результат

- Все три исходных HTML-страницы переведены на внешний JS/CSS и построение DOM-узлов без HTML из данных. Проверены также login/pilot страницы.
- Имена поля/сорта, селекционер, заметки, заголовки, error/log и архивные структуры выводятся текстом. Нет first-party HTML parsers, string evaluation, inline handlers/style и смешивания отчёта с localStorage.
- На `/workspace` работают разрешённые T02 формы собственных полей и справочника. `/report.html?job=<uuid>` открывает только свою SQL-запись, с серверной проверкой владельца до HTML. Снимок отчёта не изменяется вместе с текущими именами.
- Старые советы/экономика отображаются как неперепроверенный исследовательский архив, не разрешённые рекомендации. API-запреты новых forecast/hindcast/refresh/jobs и активных подписок сохранены для всех ролей.
- Enforced CSP без `unsafe-inline`/`unsafe-eval`: self-hosted scripts/styles, запрет script/style attributes, object/base/frame, только same-origin API. CSP есть и на отказах/redirect/500. Разрешено встраивание доверенным Arena, не произвольными preview tenants.
- Leaflet 1.9.4 закреплён локально: npm lock, воспроизводимая сборка, SHA-256/SRI и отдельный LICENSE. Нет CDN, OSM tiles и отправки координат сторонним картам; схема P01–P28 явно подписана.
- Добавлены DOM AST-check, vendor drift-check, HTTP policy tests и отдельный Chromium/Firefox CI job с fail-only screenshots. Production Node runtime не требуется; `node_modules` исключён из Docker context.
- Cookie, CSRF, CRUD и повторное открытие отчёта проверены настоящим браузером по HTTPS. XSS-сценарии повторены с отключённой CSP **в тестовом browser context**, чтобы заголовок не маскировал небезопасный рендер. В приложении такого переключателя нет.

### Проверки T03

| Проверка | Результат |
|---|---|
| Весь небраузерный набор: `pytest tests -q -m 'not browser'` | **332 passed, 2 skipped, 10 deselected**, 38 warnings, 77.79 с |
| Реальные HTTPS browser tests | **10 passed**, 2 warnings, 18.64 с |
| Пропуски небраузерного набора | Два прежних PostgreSQL-only случая; в T03 их отдельно не перезапускали. Их успешное выполнение сохранено в истории T02 |
| DOM/XSS | Сохранение, edit/delete по UUID, reload, popup, snapshot/report reopen, ошибки, Unicode и теги; CSP on/off browser contexts |
| CSP canaries | Браузер действительно заблокировал inline script, event handler, style attribute и внешний fetch |
| Cookie/ACL | HttpOnly недоступен JS; reader не пишет; другой owner получает 404 до HTML; logout/history не открывают отчёт |
| Статика | `npm run check:dom`, `npm run check:vendor`, Python Ruff F, Node syntax, `pip check`, `git diff --check` |
| Научные артефакты | `AgroCast/world` не изменён; переаудит/обучение не запускались |

Локальный браузер: **Chromium 149.0.7827.0**, Playwright **1.62.0**, temporary SQLite/HTTPS Uvicorn fixtures. CDN официальных Playwright Chromium/Firefox был недоступен по TLS; использован dev-only Chromium из npm, без отключения TLS-проверки загрузок. Firefox/официальный CI browser image и remote Actions **не проверены локально**. Caddy/Docker production rollout, внешний pentest и полная accessibility/SLO-проверка остаются открытыми.

В E2E нет production credentials. Сохранённый отчёт формирует доверенная test fixture из реально сохранённых через UI записей: новый forecast API намеренно не открывался. В одном негативном тесте подставляется JSON-ошибка API. Снимки ошибок маскируют password-поля, сетевые login traces не сохраняются. Подробности, CSP и команды — [BROWSER_SECURITY.md](BROWSER_SECURITY.md).

## История T02 · Реализованный результат

- Общий пароль T01 заменён личными аккаунтами с Argon2id и случайными серверными сессиями. Старые Basic credentials не дают доступ.
- Cookie: `__Host-`, Secure, HttpOnly, SameSite=Lax; абсолютный TTL 8 часов, максимум пять сессий пользователя.
- Origin и привязанный к сессии CSRF-токен обязательны для изменяющих запросов. Вход принимает JSON и точный настроенный HTTPS Origin. В браузере нет localStorage/sessionStorage с credential.
- Роли reader/operator/admin проверяются сервером и повторно в слое хранения. Сведения о пользователе не берутся из forwarded-заголовков.
- Поля, настройки подписок и jobs привязаны к владельцу и организации. Чужие ресурсы дают одинаковый 404, даже для admin той же организации.
- Справочник общий только внутри организации; запись — только admin. CRUD использует UUID, а не имя. Старые SQLite/JOBS не присвоены автоматически первому аккаунту и не удалены.
- Аккаунты своей организации управляются admin; отзыв/изменение прав инвалидирует сессии на всех API-инстансах. Последний активный admin защищён транзакцией и PostgreSQL row lock, включая конкурентные изменения.
- Добавлены PostgreSQL/SQLAlchemy, начальная Alembic-миграция, foreign keys и журнал identity без паролей/cookies. Миграция не запускается неявно при импорте HTTP-приложения.
- Лимит входа хранится в БД; отдельный semaphore ограничивает параллельное хеширование. Изменяющие запросы ограничены 16 KiB, ошибки 422 не возвращают секретный input.
- Compose добавляет PostgreSQL и migration job перед API, принимает DSN/пароль через файлы secrets, не публикует порты API/БД на хост. Новый CI job проверяет identity на PostgreSQL.
- Экран входа, состояние аккаунта и выход работают с серверной сессией. Управление полями/предпочтениями доступно через API; полноценные management UI остаются T21.
- Научные запреты T01 сохранены для всех ролей. Подписки пока только `active=false`; создание прогнозов/jobs, пересчёт региона и агрорекомендации не включены.

## Проверки T02 · срез до T03

Среда: Python 3.11.2, Linux, 2 vCPU. SQLAlchemy 2.0.52, Alembic 1.19.2, psycopg 3.3.5, argon2-cffi 25.1.0. Новые runtime-зависимости закреплены в `requirements.txt`; полная фиксация транзитивных зависимостей остаётся T17.

| Проверка | Результат |
|---|---|
| Полный pytest, быстрые identity fixtures на SQLite | **315 passed, 2 skipped**, 38 warnings, 92.85 с |
| Identity, владение, миграции и регрессии пилота на реальном PostgreSQL | **238 passed**, без пропусков, 2 warnings, 72.49 с |
| Два PostgreSQL-only случая | Конкурентная защита последнего admin и реальная factory из env выполнены во втором прогоне |
| Покрытие `identity/*`, `serve/security.py`, `serve/accounts.py` | **93%**, совокупное line/branch coverage; это не покрытие всего проекта |
| Негативные HTTP-сценарии | Аноним, неверная роль, same-/cross-organization IDOR, overposting, CSRF/Origin, cookie replay, revoked/expired sessions, старый Basic/Bearer, encoded paths, неизвестные маршруты |
| Хранилище | Миграция → повторное применение → schema comparison; отдельный downgrade/reapply на изолированной БД; FK не допускают чужого владельца/поля |
| Статические проверки | Ruff F для нового контура, Node syntax check, Compose JSON Schema, `pip check`, `git diff --check` |
| Код | Новый/изменённый код без комментариев и поясняющих docstring |
| Научные datasets/models в `AgroCast/world` | Не изменены |

Второй прогон выполнен на **PostgreSQL 16.2**, поставленном как вспомогательная тестовая бинарная сборка `pgserver`; он слушал только закрытый Unix socket, без TCP/внешнего порта. На каждый тест создавалась отдельная случайная схема с последующим удалением. Это настоящий PostgreSQL для SQL/транзакционных проверок, **не проверка поставки production-образа**. Параллельный запуск двух наборов влияет на измеренное время; эти числа не являются нагрузочным SLO.

В Compose/CI указан PostgreSQL 17. Сам контейнер, Caddy/TLS, браузерный E2E, remote GitHub Actions, backup/restore, внешняя безопасность и MFA/SSO в этом шаге не выполнялись. Неустранённые warnings не скрыты фильтрами. Production-секреты или реальные пользовательские аккаунты агентом не создавались; тесты использовали временные записи.

Команды воспроизведения из `AgroCast`:

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 ../.venv/bin/python -m pytest tests -q
AGROCAST_TEST_DATABASE_URL_FILE=/secure/test-only/database_url OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 ../.venv/bin/python -m pytest tests/test_identity_auth.py tests/test_identity_resources.py tests/test_identity_admin.py tests/test_identity_storage.py tests/test_pilot_access.py tests/test_pilot_deployment.py -q
```

Вторую команду использовать с отдельной тестовой БД и правом создания схем, не с production credentials. Для измерения покрытия дополнительно устанавливался `pytest-cov`; логи находятся в игнорируемом `AgroCast/data/production-work`.

## История T01

Первый шаг ввёл `closed-pilot-v1`, фиксированные 28 точек КРА, временный общий HTTP Basic, default-deny API, отключённый альтернативный API, маркировку исторического навыка и публичный минимальный liveness. На том срезе прошли **188 тестов**, новый модуль ограничения доступа имел 100% line/branch coverage. Конфигурация Compose была проверена по JSON Schema, а не запуском Docker.

В T02 общий Basic заменён сессиями, read-only режим уточнён разрешённым управлением собственными данными, policy version повышена до `closed-pilot-v2`. Соответствующие тесты T01 адаптированы к новому механизму входа; старый пароль не оставлен обходным вариантом.

## Что пока не завершено

- Единые settings и полное persistent runtime state — следующий T05. Дальнейшие browser/UX сценарии остаются T18/T21.
- Допуск научных контрактов к исполнению, миграция старого runtime state и ограниченная durable queue. Контракты T04 не включают вычисления автоматически.
- Свежесть predictor frame, временные/пространственные утечки, независимая калибровка и допуск агрорекомендаций.
- Immutable bundles, release registry, data pipeline, backup/restore, production least privilege, эксплуатационные SLO и юридические gates.

Отсутствие этих результатов не скрывается новым логином. [Release checklist](RELEASE_CHECKLIST.md) остаётся незакрытым.
