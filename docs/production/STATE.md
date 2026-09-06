# T05 · Единые настройки и постоянное состояние

Дата: **2026-09-05**. [Прогресс и проверки](PROGRESS.md) · [Identity](IDENTITY.md) · [Контракты и кэш](API_CACHE.md) · [План](PLAN.md).

**Код T05 реализован. Проверены PostgreSQL, пересоздание процесса API, перезапуск процесса БД, read-only world и восстановление. Docker build/recreate в этой среде не выполнялись.** Новые вычисления, региональный refresh и рассылка по HTTP остаются запрещёнными. Это не реализация очереди T06 и не научный допуск.

## Конфигурация и запуск

Единственный загрузчик окружения — [`RuntimeSettings`](../../AgroCast/agrocast/core/settings.py). API, numerical CLI, digest, scheduler и региональные workers используют один формат. API создаётся явной фабрикой, а чтение научного конфига, проверка PostgreSQL, подготовка каталогов, password dummy hash и logging handler выполняются в lifespan. Собственный engine и handler закрываются при остановке; внешний injected engine принадлежит вызывающему коду.

Импорт `serve.product`, CLI и maintenance-модулей не создаёт приложение, не открывает пользовательские БД, не читает bundle/config/secret и не настраивает logging. Старый ASGI endpoint `serve.api:app` остаётся инертным ответом 410, не вычислительным обходом.

| Переменная | Назначение / default |
|---|---|
| `AGROCAST_WORLD_DIR` | Абсолютный путь read-only bundle; по умолчанию `AgroCast/world` относительно пакета |
| `AGROCAST_STATE_DIR` | Абсолютный путь writable state; по умолчанию `AgroCast/data` |
| `AGROCAST_CONFIG_FILE` | Необязательный отдельный научный JSON; default — `world/config.json` |
| `AGROCAST_DATABASE_URL_FILE` | Обязательный для API файл с одним PostgreSQL psycopg DSN; содержимое не выводится |
| `AGROCAST_PUBLIC_ORIGIN` | Точный HTTPS origin без path, credentials, query и wildcard; обязателен для API |
| `AGROCAST_SESSION_SECONDS` | 300…86400, default 28800 |
| `AGROCAST_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`; default `INFO` |
| `AGROCAST_RELEASE_MANIFEST_FILE` | Явная release identity для result cache; необязателен при закрытых вычислениях, но заданный неправильный файл останавливает startup |
| `AGROCAST_PHYS_PRESET` | `land`, `state`, `land_d6`; override значения scientific config |
| `AGROCAST_REGIME_GUARD`, `AGROCAST_OSPR` | `0`, `1`, `false`, `true`; overrides scientific config |
| `AGROCAST_OSPR_W` | Конечное число 0…1; единое значение для live/offline paths |

Старые имена `AGROCAST_WORLD`, `AGROCAST_DATA`/`AGROCAST_DATA_DIR`, `PHYS_PRESET`, `REGIME_GUARD` поддержаны только как aliases. Конфликт значений останавливает запуск, а не выбирает переменную произвольно. Bundle и state не должны совпадать или содержать друг друга. Неправильный config, недоступный PostgreSQL, несовпадающая схема или незаписываемый state дают понятную причину без DSN/пароля. Прежнего placeholder-сервера при отсутствующей identity больше нет.

Из каталога `AgroCast`, после установки зависимостей и настройки PostgreSQL:

```bash
export AGROCAST_WORLD_DIR="$PWD/world"
export AGROCAST_STATE_DIR=/srv/agrocast/state
export AGROCAST_DATABASE_URL_FILE=/secure/agrocast/database_url
export AGROCAST_PUBLIC_ORIGIN=https://agrocast.example.org
python -m agrocast.identity.cli migrate
python -m agrocast.identity.cli check
python -m agrocast.serve.cli settings
python -m uvicorn agrocast.serve.product:create_app --factory --host 0.0.0.0 --port 8501
```

Нужен HTTPS reverse proxy; Secure cookie не превращается в HTTP cookie ради локального запуска. `python -m agrocast.serve.cli serve --port 8501` и `python app.py` используют ту же фабрику. Launcher больше не устанавливает пакеты автоматически и не открывает ложный HTTP login в браузере.

`python -m agrocast.state.cli settings` выводит тот же безопасный snapshot, без DSN. У numerical CLI `--state-dir` (`--data-dir` — alias) и `--world-dir` идут перед подкомандой. Явные CLI paths разрешаются относительно cwd; paths из окружения должны быть абсолютными. Numerical workers получают сериализованный эффективный `Config`, а не повторно выбирают physics flags из изменившегося окружения. Несогласованные paths snapshot/worker отклоняются.

## Разделение хранения

| Место | Содержимое | Поведение |
|---|---|---|
| PostgreSQL, volume `agrocast-db` | Organizations, users, sessions, ACL resources, catalogue, subscriptions, jobs, publications, migration journal/quarantine | Авторитетное хранилище HTTP-ресурсов |
| `world` | Исходные данные, config, модели, исторические grid/skill/value artifacts | Только чтение; наличие старого ready marker не является научной readiness |
| `state/compute` | Новые numerical artifacts, Zarr updates, offline registry, point workspaces | Все новые вычислительные записи вне bundle |
| `state/results-v1` | Параметризованный и проверяемый result cache T04 | Cache identity/complete/checksum сохранены |
| `state/offline-jobs` | Атомарные snapshots offline job до запуска и после завершения | Не очередь; нет lease/retry/resume и автоматической привязки к пользователю |
| `state/migration`, `state/backups` | Место для локальных операторских материалов | Backup всего state необходимо хранить **вне** state, иначе возникнет рекурсия |

`Config.artifact_path()` читает локальный overlay, затем разрешённую bundled-модель. `artifact_dir` используется для записи в state. Для отдельной внешней точки bundled-модели не подставляются молча; общие input features/Zarr читаются из common state overlay и затем из bundle. Векторные операции и модели не объявлены переаттестованными.

Обновление live ledger использует copy-on-write: первая запись в state не скрывает прежние bundled записи. Неиспользуемая калибровка сохраняется как явно неактивная, чтобы удаление локального файла не включало старую bundled-калибровку. Offline audit/refit/value/grid/snow commands больше не публикуют результаты прямо в `world` по умолчанию. Подготовка и научный допуск нового bundle остаются дальнейшими задачами.

Compose сохраняет `/app/data` в прежнем named volume, монтирует `../AgroCast/world:/app/world:ro`, делает корневую ФС app read-only и выделяет `/tmp` как tmpfs. App/migrate имеют одинаковые canonical paths. PostgreSQL и state volumes необходимо сохранять вместе с прежним Compose project name. **Не используйте `down -v`, volume prune или новый project name как способ обновления.** Файл конфигурации Compose проверен статически; это не доказательство фактического container recreation.

## Миграция схемы

[`0003_persistent_state`](../../AgroCast/migrations/versions/0003_persistent_state.py) идёт после `0002_crop_revision`:

- `publications` с owner/org, ссылкой на job того же владельца, checksum и сохранённым JSON;
- `migration_runs` с уникальным namespace, source/mapping hashes и отчётом сверки;
- `legacy_records` с полным исходным содержимым, явным mapping, disposition и ссылками/хэшами нормализованных целей.

Существующие таблицы пользователей, каталога, подписок и заданий не пересоздаются. Publications доступны только через owner-scoped GET `/api/publications` и `/api/publications/{UUID}`. HTTP update/delete/create публикаций отсутствуют; удаление связанного job возвращает 409. Catalogue остаётся общим внутри организации. Сохранены дополнительные legacy-поля сорта: группа спелости, окно сева и площадь; UI не теряет их при редактировании имени.

Перед обновлением **старой** схемы сначала сделайте PostgreSQL-native backup. JSON backup T05 принимает только текущую мигрированную схему и не заменяет pre-upgrade dump. Пример для уже работающего Compose, из корня репозитория:

```bash
umask 077
mkdir -p /secure/recovery/t05
set -e
docker compose -f deploy/docker-compose.yml stop app caddy
docker compose -f deploy/docker-compose.yml exec -T database pg_dump -U agrocast -d agrocast --format=custom > /secure/recovery/t05/before-upgrade.dump
docker compose -f deploy/docker-compose.yml exec -T database pg_restore --list < /secure/recovery/t05/before-upgrade.dump
sha256sum /secure/recovery/t05/before-upgrade.dump > /secure/recovery/t05/before-upgrade.dump.sha256
docker compose -f deploy/docker-compose.yml build app migrate
docker compose -f deploy/docker-compose.yml run --rm migrate
```

Это deployment runbook, не выполненная здесь Docker-проверка. Проверьте backup на отдельном PostgreSQL до удаления прежнего окружения. `downgrade` удаляет новые архивные таблицы и не является безопасным production rollback.

## Перенос legacy: только на снимке и с явным решением

Нет автоматического присвоения записей первому admin, tenant или вошедшему пользователю. SQLite и localStorage не дают доказанного владельца. Сначала остановите все legacy writers. Пока прежний процесс жив, выгрузите его in-memory jobs: после остановки несохранённые записи нельзя восстановить.

CLI принимает отдельный каталог источников с `crops.db` и `registry.sqlite`, в том числе вложенными, и явные JSON arrays полей/заданий. Никакой миграции публичным HTTP endpoint нет.

```bash
python -m agrocast.state.cli snapshot-legacy --source /srv/agrocast/legacy-readonly --output /secure/recovery/t05/legacy-snapshot --fields-json /secure/recovery/t05/fields.json --jobs-json /secure/recovery/t05/jobs.json
python -m agrocast.state.cli verify-snapshot --snapshot /secure/recovery/t05/legacy-snapshot
```

Если in-memory jobs **действительно отсутствуют**, вместо `--jobs-json` требуется явное `--no-in-memory-jobs`. Новый offline helper сохраняет snapshots; экспортировать их без исполнения можно командой `export-offline-jobs --output /secure/recovery/t05/jobs.json`. `running` в экспортированном файле — последний сохранённый статус, не доказательство продолжающегося исполнения.

Snapshot создаётся через SQLite `Connection.backup`, включая подтверждённые WAL-записи, плюс копии JSON exports. Manifest содержит hashes и counts; при чтении заново сверяется содержимое снимков SQLite/JSON. Исходные БД не модифицируются. Неполный snapshot без корректного manifest не импортируется. Исходные технические таблицы также сохраняются, а не теряются при извлечении пользовательских записей.

В `mapping.template.json` все записи изначально quarantined и `reviewed=false`. Скопируйте шаблон, проверьте **каждую** запись и только после этого установите `reviewed=true`. Возможные решения:

```json
{
  "action": "quarantine",
  "reason": "Владелец не подтверждён"
}
```

```json
{
  "action": "import",
  "owner_id": "OWNER_UUID",
  "organization_id": "ORGANIZATION_UUID"
}
```

UUID placeholders нужно заменить существующими идентификаторами. `records` — объект, ключи которого берутся **из конкретного manifest/template**, например `crops.db:variety:1`, `registry.sqlite:subscriptions:1`, `fields.json:field-1`, `jobs.json:job-42`. Ни пропуски, ни дополнительные ключи не разрешены.

Дополнительные обязательные решения:

- Для legacy field — `point_id`, если его нет в экспорте. Координаты должны точно соответствовать этому пилотному пункту. Внепилотные координаты/культура идут в карантин, не округляются и не переназначаются.
- Для subscription — `start_month`, `season_len` и ровно один `field_key` из snapshot или существующий `field_id` того же owner/org. Неизвестный исходный месяц не угадывается. Horizon/mode/variables сохраняются; active принудительно false, прежнее значение остаётся в raw archive.
- Для сорта в subscription — `variety_key` или `variety_id` плюс подтверждённая `variety_revision`; проверяется организация и текущая версия.
- Для queued/running/pending job — `interrupt=true`. Импортированный job станет failed с `legacy_job_interrupted`, прежний статус/params/log/result сохранятся в архиве. Никакого запуска или retry.
- Completed jobs и forecasts создают job/publication с исходным report. Неправильный JSON/неподдерживаемые данные требуют карантина, а не незаметного исправления.
- Технические records остаются quarantined. Неизвестные пользователи, org mismatch, отсутствующие ссылки и несогласованные контракты отклоняют **всю** транзакцию.

Применение на отдельном подготовленном PostgreSQL, с остановленными writers:

```bash
python -m agrocast.state.cli import-legacy --snapshot /secure/recovery/t05/legacy-snapshot --mapping /secure/recovery/t05/approved-mapping.json --namespace legacy-2026-09-05 --backup /secure/recovery/t05/before-import.json --report /secure/recovery/t05/import-report.json --maintenance-confirmed
```

CLI обязательно делает эксклюзивный backup PostgreSQL **до** import. В одной транзакции проверяются owner/FK/response contracts, counts, hashes записанного нормализованного содержимого и полного raw archive. Namespace определяет стабильные UUID: повтор того же snapshot/mapping не создаёт дубликаты; другой snapshot/mapping с прежним namespace отклоняется. При повторном CLI-запуске используйте новые имена backup/report. `already_applied=true` относится к завершённому первоначальному импорту, а не к запрету последующего законного редактирования resources.

Сохраните source snapshots, approved mapping, pre-import backup и report **вне Git**, с доступом только у уполномоченных операторов. Карантин не появляется в пользовательском каталоге и не становится разрешённым прогнозом.

## Backup / restore текущего состояния

Остановите API, offline writers, scheduler и будущие workers. Пока они остановлены, сделайте **оба** backup в recovery-каталог вне state:

```bash
python -m agrocast.state.cli backup --output /secure/recovery/t05/database.json
python -m agrocast.state.cli backup-runtime --output /secure/recovery/t05/runtime --maintenance-confirmed
```

PostgreSQL backup использует согласованный `REPEATABLE READ` snapshot, фиксирует schema revision, table counts/hashes и общий checksum. Он содержит пользователей и password hashes, ACL resources и quarantine. Сессии и login limits сознательно исключены. Размер этого JSON-формата ограничен 128 MiB; для больших БД нужен отдельно проверенный PostgreSQL-native backup/restore.

Filesystem backup сохраняет bytes, список файлов/каталогов, counts/checksums; отвергает symlinks, special files, рекурсивный backup и обнаруженные изменения источника. Остановка writers обязательна: проверка файлов не заменяет распределённый snapshot живой системы. Создание backup не перезаписывает существующий путь. Файлы создаются с правами 0600, новые private-каталоги — 0700. Шифрование, удалённая копия, retention, расписание и RPO/RTO этим инструментом **не обеспечиваются**.

Восстановление — только в отдельную пустую БД с текущей схемой и пустой state. Сначала задайте `AGROCAST_DATABASE_URL_FILE` и `AGROCAST_STATE_DIR` для **целевого**, не исходного окружения:

```bash
python -m agrocast.identity.cli migrate
python -m agrocast.state.cli restore --backup /secure/recovery/t05/database.json --empty-target-confirmed
python -m agrocast.state.cli restore-runtime --backup /secure/recovery/t05/runtime --empty-target-confirmed
python -m agrocast.identity.cli check
python -m agrocast.serve.cli settings
```

DB restore выполняется транзакционно, сверяет содержимое и требует пустые таблицы. Runtime restore сначала проверяет manifest/paths, копирует и сверяет staged files, затем заменяет только пустую директорию. Nonempty target не очищается. Запускайте сервис только после успешной проверки **обоих** хранилищ и подключения нужного read-only bundle.

Старые cookies после DB restore не работают: сессии не восстанавливаются, нужен новый вход. Offline snapshots после restore не исполняются. Это сохранение состояния, не durable execution queue. PostgreSQL-native dump может содержать sessions: при таком отдельном восстановлении их нужно явно отозвать до открытия доступа.

## Фактически выполненная репетиция

На изолированных копиях `world/registry.sqlite` и локального legacy `data/crops.db`:

- **177 исходных records: 52 forecasts, 7 varieties, 118 технических записей.** В этих источниках нет subscriptions или in-memory jobs; их перенос проверен отдельными интеграционными fixtures.
- Подтверждённой карты владельцев для этих реальных legacy records нет: **177 сохранены в quarantine, 0 присвоены пользователям**. Ничего не потеряно и не принято за принадлежащий первому пользователю ресурс.
- Выполнены backup → import на отдельном PostgreSQL 16.2 schema → сверка → idempotent repeat → backup → restore в другой пустой schema.
- Затем PostgreSQL **реально остановлен и запущен заново**; counts и hashes обеих схем совпали с backup. Checksums исходных файлов не изменились.
- Отдельная mapped fixture проверила сохранение field, crop, inactive subscription, трёх jobs и двух publications; доступ владельца/чужого owner, PostgreSQL restore и две независимые интерпретации API-процесса.

Локальные материалы: `AgroCast/data/production-work/t05-rehearsal/`, `t05-rehearsal.log`, `t05-restart.log`; они ignored и не загружаются в PR. Итоговые regression-числа — в [PROGRESS.md](PROGRESS.md).

## Что ещё не доказано

Docker/Compose build, actual volume recreation, PostgreSQL 17 image, Caddy rollout и аварийное восстановление на production-инфраструктуре не запускались здесь. PostgreSQL 16.2 process/SQL tests и static YAML validation не заменяют эти проверки. Не реализованы очередь/outbox T06, readiness T07, научные исправления и доверенный release/provenance process T14. Сохранение данных и passing smoke tests не подтверждают навык прогноза или безопасность агрорекомендаций.
