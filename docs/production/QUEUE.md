# Очередь вычислений и ограничение ресурсов (T06)

## Статус и границы допуска

Механизм очереди реализован и протестирован, но **приём вычислительных заданий закрыт**:
`AGROCAST_QUEUE_INTAKE=false` по умолчанию, а пилотный допуск расчётов отдельный gate —
`x-pilot-computation-enabled` в контрактах всегда `false`. Очередь — инфраструктура: она не
включает научный запуск, рассылки или агрорекомендации. Флаг intake предназначен только для
dev/test конфигураций и должен оставаться закрытым до научных и release gates (T08/T13/T14).

## Модель состояний

Задание живёт в таблице `jobs` (общая identity-схема, миграция `0004_durable_queue`) со
состояниями `queued → running → succeeded | failed | cancelled`. Каждый переход — событие в
append-only таблице `queue_events` с монотонным `sequence` внутри задания (UNIQUE
`(job_id, sequence)`); события читаются клиентом через `GET /api/jobs/<id>/events?since=N`
и не перезаписываются. Публикация результата — outbox-строка в `publications`: создаётся
только для top-level видов (`point_forecast`, `point_hindcast`, `region_field`) и только один
раз на задание, поэтому повторная доставка результата не дублирует выпуск.

Идентичность результата сохраняется полностью: параметры допуска (`spec`, `identity`,
`releases`, `config_snapshot`) лежат в `jobs.data.params`, контрольная сумма канонического
JSON результата — в `jobs.result_checksum`, ссылка на кэш — в `result_ref`
(`results-v1/<identity-key>.json`). Legacy-строки `jobs` (без `queue_kind`) очередью не
видятся и не трогаются.

## Идентичность повторных admission (dedup)

Дедупликация считается по SHA-256 полному identity-ключу: для точки — `ResultIdentity.key()`,
для региона — `region_identity(...).key()`. Частичный уникальный индекс `uq_jobs_active_dedup`
по `(organization_id, dedup_sha256)` действует среди `queued`/`running`: повторный admission
с идентичным запросом возвращает тот же `job_id` с заголовком `X-Deduplicated: true` и не
создаёт событий. После терминального состояния строки дедуп не блокируют — новый admission
создаёт новое задание, которое при неизменных входах завершится мгновенно из кэша
(`published_from_cache`, одна копия файла кэша на оба задания). Гонка двух реплик закрыта
повторной проверкой при `IntegrityError`.

## Lease, heartbeat, возврат в очередь

`claim` атомарно выбирает `queued`-задание (PostgreSQL: `FOR UPDATE SKIP LOCKED`; SQLite:
`UPDATE ... RETURNING`), ставит `lease_owner`/`lease_expires_at` и увеличивает `attempts`.
Глобальная ёмкость `queue_global_slots` ограничивает одновременные расчёты поверх
per-user квоты. Worker продлевает lease heartbeat'ом и попутно пишет строки лога
(кап `queue_log_lines`). Упавший worker не блокирует очередь: `reap_expired_leases`
возвращает просроченные задания — отменённые закрывает как `cancelled`, остальные ставит
обратно в `queued` с backoff (или `failed lease_expired`, если попытки исчерпаны).
Завершение чужим/просроченным владельцем невозможно: `complete`/`fail`/`finalize_cancelled`
требуют совпадения `lease_owner` и состояния `running` (иначе `LeaseLost`).

## Retries, deadline, лимит результата

Видимая ошибка допуска: `429 job_quota_exceeded` (переполнение user-квоты активных заданий)
и `503 queue_saturated` (глубина очереди `queue_max_queued`) — оба с `Retry-After`,
без создания потоков и без записи в БД. Ошибки исполнения ретраятся с экспоненциальным
backoff `retry_seconds · 2^(attempt−1)` до `retry_cap_seconds` при `max_attempts` (1..5);
фатальные (`retryable=false`, например рассинхрон grid или corrupt-результат) — терминальны
и каскадом отменяют детей/родителя. `deadline_seconds` засекается worker'ом: по нему детей
и внуков убивает группа, задание ретраится. Результат — strict JSON ≤ 2 MiB с проверкой
checksum staging-файла; inline-репорт ≤ 512 KiB иначе остаётся ссылка.

## Worker и песочница

`python -m agrocast.queue.worker run` (systemd/compose) — цикл reap → очистка staging → claim.
Каждое задание исполняется отдельным процессом `-m agrocast.queue.exec`: своя session/
process group (отмена и deadline убивают группу целиком, включая grandchildren — это
проверяется тестом), stdin-spec 0600, канонический stdout NDJSON-событиями, результат —
staging-файл `queue-staging/<job>.json` c checksum, права 0600. Ребёнок не получает доступ
к БД и `AGROCAST_*` переменным окружения. Песочница ребёнка: `PR_SET_NO_NEW_PRIVS`,
seccomp-denylist (mount/ptrace/socket-листь и т.п.; `clone3` не блокируется — потоки numpy),
RLIMIT CPU/FSIZE/NOFILE, опциональный RLIMIT_AS. BLAS-потоки принудительно
`queue_blas_threads` (по умолчанию 1) через OPENBLAS/MKL/OMP — это ограничивает и регион
subjobs: ячейки считаются тем же механизмом с гейтом доступных слотов.

## Операционный runbook

- Запуск: compose-сервис `worker` (read-only rootfs, tmpfs /tmp, `cap_drop: ALL`,
  `no-new-privileges`, volumes: `agrocast-data` rw для staging/кэша, `world` ro;
  Docker-профиль seccomp остаётся профилем по умолчанию — свой профиль не переопределяется).
- Наблюдение: `python -m agrocast.queue.worker stats` (или `GET /api/queue/stats`, admin):
  глубина, running, исчерпанные lease, включённый intake.
- Retention: `python -m agrocast.queue.worker sweep` — удаляет staging неактивных заданий и
  терминальные строки старше `queue_retention_days` (события каскадом). Файлы кэша
  результатов не удаляются — они переиспользуются dedup-путём.
- Перезапуск worker'а безопасен в любой момент: lease протухнет, задание вернётся в очередь
  с прежним `job_id` и полной историей событий; статус клиента не меняется.
- Отмена: `POST /api/jobs/<id>/cancel` (идемпотентна): `queued` ⇒ сразу `cancelled` + каскад
  детей; `running` ⇒ флаг отмены, worker убивает группу процессов, пишет `cancelled` и
  отбрасывает поздний результат.

## HTTP-контракты допуска

`POST /api/prepare` и `POST /api/region/refresh` при закрытом intake отвечают
`403 pilot_operation_disabled`; при открытом: shape → pilot-target → releases-manifest
(`503 release_identity_unavailable`, если файл release identities недоступен) → dedup →
квоты → `202 {"job", "status": "queued", "status_url"}` + `Location`. `GET
/api/queue/jobs/<id>` — durable-представление (для legacy-строк 404). Роли и CSRF/Origin
сохраняются: cancel — write-роль владельца, events/статусы — authenticated owner или admin.

## Покрытие приёмки тестами

| Свойство | Тест |
| --- | --- |
| Полный жизненный цикл, slots, backoff, reap, stale-owner, cancel, discard позднего результата, region first-wins, non-retryable каскад, retention sweep | `tests/test_queue_core.py` (17) |
| Закрытый intake, contracts/capabilities, полная identity в admission, dedup 202/X-Deduplicated, kind-mismatch, квоты 429/503 + Retry-After, доступность release-manifest, owner-scope, две реплики — один статус, CSRF/Origin | `tests/test_queue_api.py` (13) |
| Реальный subprocess-worker на PostgreSQL: вычисление точки с публикацией идемпотентный re-publish, kill -9 worker'а посреди задачи → requeue с тем же id, cancel убивает всю группу, seccomp/RLIMIT/BLAS=1 в ребёнке, deadline, исчерпание ретраев, sweep/stats CLI | `tests/test_queue_worker.py` (7) |
| Миграция 0004: upgrade/downgrade roundtrip с legacy-данными, partial-индекс дедупа, UNIQUE событий, сверка живого заголовка | `tests/test_queue_migration.py` (4) |

Запуски docker-compose и systemd на этой машине не выполнялись (нет Docker): конфигурация
проверена декларативно (YAML + согласованность env), фактический container rollout — часть
T12/T14.
