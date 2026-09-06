# Прогресс реализации production-плана

[План](PLAN.md) · [Scope пилота](PILOT.md) · [Identity и запуск](IDENTITY.md) · [DOM/CSP](BROWSER_SECURITY.md) · [HTTP/кэш](API_CACHE.md) · [Settings/state](STATE.md) · [Очередь](QUEUE.md) · [Десктоп](DESKTOP.md) · [Исходный аудит](AUDIT.md) · [Release gates](RELEASE_CHECKLIST.md)

## Состояние на 2026-09-06

**T01–T06 реализованы и локально проверены. T07–T24 ещё не завершены. Container rollout/recreation для T05 и фактический Docker-запуск worker'а для T06 не проверены; production-релиз не разрешён.**

Работа идёт по одному пункту с проверкой результата. T06 добавляет persistent очередь с leases, dedup, квотами, retries/deadline, изолированным worker-процессом и идемпотентной публикацией; приём заданий закрыт (`AGROCAST_QUEUE_INTAKE=false`) и научный gate не затронут. Это не readiness T07 и не допуск расчётов.

| Пункт | Статус | Результат |
|---|---|---|
| T01 · Scope закрытого пилота | Реализован; ограничения сохранены | Новые прогнозы/агрорекомендации запрещены API, исторические результаты помечены неперепроверенными |
| T02 · Identity / роли / ownership | Реализован; проверен на PostgreSQL | Личные аккаунты, server sessions, CSRF, reader/operator/admin, owner/organization и миграция |
| T03 · DOM / CSP | Реализован; Chromium-проверки пройдены | Текстовые DOM-узлы, enforced CSP, локальный Leaflet, реальные HTTPS browser tests |
| T04 · HTTP / cache | Реализован; локальные проверки пройдены | Строгие модели/ошибки, crop revision, полный ключ и атомарный региональный cache |
| T05 · Settings / persistent state | Реализован; локально проверен; Docker-приёмка не выполнена | Общие settings, factory/lifespan, read-only bundle, migration/quarantine, DB/runtime backup и restore |
| T06 · Устойчивая очередь | Реализован; проверен на PostgreSQL и SQLite; Docker не запускался | Persistent state machine, outbox-публикации, lease/heartbeat, retries/deadline, quotas/dedup, retention, изолированный worker; intake закрыт |
| T07–T24 | Ожидают исполнения | Подготовительные изменения не закрывают соответствующие задачи |

## T06 · Реализованный результат

Очередь (`agrocast/queue/`) переведена с потокового `JOBS`-реестра на persistent state machine:
`jobs` расширен 14 queue-колонками миграцией `0004_durable_queue` (upgrade/downgrade roundtrip
покрыт тестом), события — append-only `queue_events` с монотонным sequence, выпуск —
идемпотентный `publications`-outbox (один на задание). Admission `POST /api/prepare` и
`POST /api/region/refresh` возвращают `202` с `Location` на `/api/jobs/<id>`; `GET
/api/queue/jobs/<id>`, `GET /api/jobs/<id>/events`, `POST /api/jobs/<id>/cancel` и admin
`GET /api/queue/stats` дополняют контур; CSRF/roles/owner-scope наследуются T02,
legacy-строки `jobs` очередью не видятся. Дедупликация — partial unique индекс
`(organization_id, dedup_sha256)` среди `queued/running` по полному identity-ключу результата;
гонка реплик закрыта повторной проверкой при `IntegrityError`. Квоты: пер-юзер и глобальные
слоты, глубина очереди — `429 job_quota_exceeded` / `503 queue_saturated` с `Retry-After`.
Worker (`python -m agrocast.queue.worker`) исполняет каждое задание отдельным процессом
`agrocast.queue.exec` в собственной process group с `PR_SET_NO_NEW_PRIVS`, seccomp-denylist,
RLIMIT CPU/FSIZE/NOFILE (опционально AS), BLAS-потоками `=1` и без доступа к БД и
`AGROCAST_*`-переменным; результат — strict JSON ≤ 2 MiB через staging-файл 0600 с проверкой
checksum; отменa/deadline убивают группу целиком, поздний результат отбрасывается; lease
истекает — задание возвращается в очередь с backoff. Retention — `sweep`: staging и
терминальные строки старше `queue_retention_days`, кэш результатов сохраняется. Compose
получил сервис `worker` (read-only rootfs, tmpfs, `cap_drop: ALL`, `no-new-privileges`,
world ro / data rw). CI-джоб `identity-postgresql` расширен четырьмя queue-файлами.

**Фактические прогоны (эта машина, PostgreSQL из `pgserver` только на Unix socket):**
полный non-browser набор `python -m pytest tests -q` — **568 passed, 13 skipped, 0 failed**
(184 с, SQLite; skipped — browser/policy без playwright); целевая батарея на PostgreSQL —
**376 passed, 0 failed** (190 с): queue-файлы целиком (core 17, api 13, worker 7, migration 4),
identity-набор, contracts, access, state migration; очередь на SQLite прогнана 6+ раз подряд
без flaky; worker-тесты поднимают отдельный `pgserver` и реальный subprocess-воркер
(kill -9 посреди задачи ⇒ requeue с тем же id; cancel не оставляет дочерних процессов —
проверено по `/proc`; seccomp/RLIMIT/BLAS проверены в ребёнке; идемпотентный повторный
выпуск из кэша не дублирует публикацию). `ruff check --select F,E9` чистый; новые и
изменённые py-файлы без комментариев и docstring (ast-скан). Найденные по ходу реальные баги
исправлены: `ensure_point` в `region_cell`, `ProcessLookupError` при terminate уже мёртвой
группы, `JobRecord.extra="forbid"` падал на queue-колонках, верификация legacy-импорта
сравнивала строки целиком вместо известных колонок, текстовый stdout-пайп ребёнка — переход на
non-blocking `os.read`.

**Что это не закрывает:** Docker в среде нет — compose-сервис проверен декларативно (YAML +
согласованность env), фактический контейнерный rollout, systemd-юниты и seccomp-профиль Docker
не выполнялись. Приём (`x-pilot-computation-enabled`) остаётся закрытым; worker готовит и
публикует только принятые admission-задания. Region-поле через реальный worker (cells/merge
через песочницу) проверено на уровне coordinator/first-wins, полный 28-точечный регион-прогон в
песочнице не выполнялся из-за стоимости; `region_cell`/`region_merge` идут через те же пути.
Нагрузочного SLO-теста очереди не было (число 200 в depth — конфигурация, не измерение).

## T05 · Реализованный результат

- Один `RuntimeSettings` для API/CLI/worker, валидируемые canonical paths/aliases, numeric overrides и безопасный snapshot. Worker использует захваченный effective config; ошибки startup не выводят DSN/пароль.
- Убран глобальный `product.app`, чтение env/config/БД и настройка logging из import-time пути. Основной entrypoint — `agrocast.serve.product:create_app --factory`. Launcher делегирует общей CLI; engine/handler закрываются lifespan.
- `world` — read-only bundle; новые Zarr/artifacts/registry/point state — только в writable state. Общие обновлённые inputs читаются и внешними точками, но чужие bundled-модели им не подставляются. Исправлены copy-on-write live ledger и отключённая калибровка поверх старого bundle.
- Миграция `0003_persistent_state`: owner-scoped publications, migration journal и quarantine/raw archive. Старые таблицы не пересоздаются. Catalogue legacy-поля проходят через DTO и UI без потери при edit.
- SQLite backup snapshots + явные JSON exports; обязательное решение о наличии in-memory jobs и mapping для каждой записи. Никакого присвоения первому вошедшему. Неизвестный start month/field/owner не угадывается. In-flight jobs требуют explicit interruption, а не restart.
- Транзакционный/idempotent import со сверкой counts/hashes исходного архива и нормализованных rows. CLI требует pre-import backup и maintenance confirmation.
- Private DB backup/empty-target restore с table checksums и без восстановления sessions; отдельный filesystem backup/staged restore. Offline job snapshots сохраняются, но не превращены в очередь.
- Все T01–T04 запреты HTTP compute/refresh/delivery, ownership, CSRF, CSP и exact cache identities сохранены.

### Выполненные проверки T05

| Проверка | Результат |
|---|---|
| Полный non-browser pytest | **527 passed, 3 skipped, 10 deselected**, 38 warnings, 193.70 s |
| PostgreSQL | **426 passed**, 2 warnings, 141.27 s; настоящий PostgreSQL 16.2, включая три PG-only случая |
| Browser | **10 passed**, 2 warnings, 26.16 s; Chromium 149.0.7827.0 + Playwright 1.62.0, настоящий HTTPS; включая сохранение дополнительных полей сорта после редактирования |
| Выбранные T05-модули | **87% combined line/branch coverage**: settings/config, lifespan, state tools и atomic writer; не coverage всего проекта |
| Read-only bundle | Полные forecast smoke на 60 MiB копии world с 0444/0555; контрольная запись получает PermissionError, все file checksums после тестов прежние |
| Settings / import | API/CLI/region worker configuration parity, защита от изменения env после snapshot, несовпадающих paths, bad configuration и import-time DB/files/hash/logging |
| Schema / restart / restore | Alembic ↔ metadata comparison, повторные upgrade, изолированный downgrade/reapply, PostgreSQL backup/restore и два отдельных API-процесса с теми же resources/checksums |
| Legacy rehearsal | **177 actual legacy records**: 52 forecasts + 7 varieties + 118 technical; все 177 явно quarantined, т.к. owner mapping не предоставлен. Backup → import → reconcile → repeat → restore → реальный restart PostgreSQL; hashes/counts совпали, оригиналы не изменились |
| Mapped import | На отдельной fixture: 1 field, 1 crop, 1 inactive subscription, 3 jobs, 2 publications; owner/org negatives, interrupted running job, rollback при плохой карте, source corruption и idempotency |
| Filesystem recovery | Bytes/directories/counts/checksums, private files, отказ nonempty target, symlink/traversal/corruption/partial-copy cases; jobs не возобновляются |
| Статические проверки | 67 changed Python files: syntax, no comments/docstrings, Ruff F; 139 local Markdown links/UTF-8/headings; DOM/vendor checks, pip check и Compose schema пройдены; `world` не изменён |
| Docker | **Не выполнено**: в sandbox нет Docker executable/socket. YAML/Compose checks не заменяют build/recreate/volume/restore в контейнерах |

Полные логи: `AgroCast/data/production-work/t05-full-final.log`, `t05-postgres-final.log`, `t05-browser-final.log`, `.coverage-t05`, `t05-rehearsal.log`, `t05-restart.log`. Test/recovery data ignored, не включены в Git. Часть проверок шла параллельно; времена не являются performance benchmark.

38 warnings оставлены видимыми: прежние numerical/pandas warnings и два предупреждения Starlette/AnyIO. PostgreSQL 17 из Compose, Docker build/recreate, Caddy и Firefox локально не проверены. [Runbook и границы T05](STATE.md).

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

## T07 · Readiness и явная свежесть входов (2026-09-06)

Разделены `/health/live` (процесс) и `/health/ready` (данные): публичный GET со строгим
контрактом `ReadinessResponse`; 503 при отсутствии/повреждении обязательных источников
(`daily_region`, `fields_monthly`), неполной последней предсказорной строке, старом фрейме,
битом/несовместимом бандле (`bundle:contract_mismatch`, `manifest_ahead_of_store`) и
недоступной БД; опциональные источники (`sst`, `strat_snow`, `oisst_boxes`, `regimes`) дают
явно маркированную деградацию без отказа. Даты — из zarr-координаты времени и `generated_at`
артефактов навыка; файловый mtime критерием не является (для runtime-поля возраст по mtime
печатается с явной оговоркой). `scripts/zarr_freshness.py` переписан поверх того же
`agrocast/serve/readiness.py` (единая таблица лимитов; missing/error обязательного — exit 1),
все лимиты ≥ удвоенного недельного cron тестом согласованности. Gate `align_issue` в
оркестраторе: явный месяц выпуска требует наличие месяца `start-1` в предсказорном фрейме,
иначе `IssueFreshnessError` (веб — ошибка задачи, десктоп — 422 `issue_inputs_mismatch`);
неявный запуск анкоруется к последней полной строке входов. Десктоп отдаёт `sources_through`
и ограничивает поле месяца (`max` = fields_monthly+1). compose: healthcheck app на
`/health/ready`, caddy ждёт `service_healthy`; Dockerfile HEALTHCHECK остался на live.

Проверено: новые `tests/test_readiness.py` (17 тестов, синтетический бандл, фиксированное
время), обновлённый десктопный сценарий (честный месяц `2026-03` считается, `2026-10`
отклоняется 422), `test_region_ops`/`test_pilot_deployment` с восстановленным
`region_freshness` и валидным compose; полный набор — зелёный (см. сноску ниже). Научный
допуск и приём веб-заданий не изменялись: readiness готовит честный вход, gates T06/T14
остаются закрытыми.

## Десктоп-контур D · D01–D03 (2026-09-06)

Локальный режим без браузера и без логина: `python -m agrocast.desktop` или собранный
`AgroCast(.exe/.app)`; окно QtWebEngine, внутри — тот же uvicorn/FastAPI на 127.0.0.1 с
sqlite-состоянием в `~/.agrocast`, предзаполненным владельцем и CSRF/origin-ослаблениями
только в desktop-ветке `AccessGuard` (веб-контур не менялся). Мир читается из бандла
внутри установщика (≈60 МБ), запись — только в state. Три блока на экране: прогноз,
скачанные данные (файлы `world`, `state/compute`, кэш результатов с датами из конвертов)
и объяснение; кнопка расчёта нового прогноза, кэш — мгновенный повтор.

Проверено локально (Linux): `tests/test_desktop.py` — 8 тестов, включая реальный offline
расчёт (10,4 с, повтор `cached=true` за 7 мс, ровно один кэш-файл, очереди не тронуты) и
403 закрытых веб-эндпоинтов; полный набор `tests -q` — 576 passed, 13 skipped; смоук
встроенного uvicorn-сервера с реальным HTTP.

Проверено CI (`desktop-builds`): матрица ubuntu/macos/windows полностью зелёная — тесты,
смоук встроенного сервера, PyInstaller-сборки и артефакты на каждую ОС (362/751/633 МБ с
бандлом мира и иконкой внутри). Windows-прогон вскрыл и после исправлений закрыл пять
дефектов переносимости (directory fsync, cp1251 в тестовых подпроцессах, права 0600,
AF_UNIX в лаунчере uvicorn, Pillow для иконки EXE). Подробности и границы — `DESKTOP.md`.

## Что пока не завершено

- Единые settings и persistence реализованы в T05; фактический container rollout/recreation остаётся непроверенным. Дальнейшие browser/UX сценарии — T18/T21.
- Научный допуск расчётов не открыт: очередь T06 реализована как инфраструктура с закрытым приёмом (`queue_intake=false`), включение требует научных и release gates (T08/T13/T14).
- Свежесть predictor frame, временные/пространственные утечки, независимая калибровка и допуск агрорекомендаций.
- Immutable bundles, release registry, data pipeline, backup/restore, production least privilege, эксплуатационные SLO и юридические gates.

Отсутствие этих результатов не скрывается новым логином. [Release checklist](RELEASE_CHECKLIST.md) остаётся незакрытым.
