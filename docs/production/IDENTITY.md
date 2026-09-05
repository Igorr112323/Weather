# T02 · Личные учётные записи, роли и владение

Дата: **2026-09-05**. Identity реализована в T02; текущая политика после T04: **`closed-pilot-v4`**. [Защита DOM/CSP](BROWSER_SECURITY.md).

[План](PLAN.md) · [Действующий scope](PILOT.md) · [Прогресс и проверки](PROGRESS.md)

## Что реализовано

Общий пароль T01 заменён локально управляемыми учётными записями. Пароли хешируются **Argon2id** через `argon2-cffi`: 64 MiB, 3 прохода, parallelism 1. Минимальная длина нового пароля — 15 символов, максимальная — 128; пароль не обрезается и не нормализуется. Следует использовать менеджер паролей или длинную непредсказуемую парольную фразу.

Серверная сессия — случайный 256-битный opaque token. В PostgreSQL сохраняется только SHA-256 токена, не сам cookie. Каждый запрос проверяет срок сессии, текущую роль, активность пользователя и организации. Нет доверия к `X-Forwarded-User`, `X-Auth-Request-User`, заявленной роли, loopback, Bearer/JWT без проверки или старому HTTP Basic.

Cookie `__Host-agrocast_session` имеет `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, без `Domain`. Срок по умолчанию — 8 часов, абсолютный, без бесконечного продления. Допускается до пяти активных сессий пользователя; новый вход вытесняет старые. Выход удаляет серверную сессию. Смена пароля, роли или активности отзывает все сессии затронутого пользователя, в том числе для других API-инстансов.

Это полноценная локальная identity для контролируемого пилота, **не OIDC/SSO и не MFA**. Выдача начальных аккаунтов и паролей требует доверенного администратора и безопасного канала передачи. Production-развёртывание, внешний pentest и финальные release gates не объявлены выполненными.

## Модель владения

- Один пользователь принадлежит одной организации и имеет одну роль.
- Имя пользователя уникально в пределах экземпляра сервиса и приводится к нижнему регистру; ID пользователя и организации — UUID.
- Организация и владелец ресурсов определяются сервером по сессии. Прислать `owner_id`, `organization_id`, `id` или `role` в теле поля и переприсвоить ресурс нельзя.
- **Поля, настройки подписок и задачи приватны владельцу.** Администратор той же организации не получает автоматического права читать или удалять чужие поля/отчёты.
- Справочник сортов общий только внутри организации. Читать его могут её участники, изменять — только её администраторы. `owner_id` записи справочника обозначает создателя; это намеренное исключение из персональной видимости.
- Составные foreign keys не позволяют связать ресурс с владельцем из другой организации или подписку с полем другого владельца.
- Изменять организацию/владельца существующих записей по HTTP нельзя. Межорганизационный перенос, account deletion/export и назначение владельцев старым данным остаются отдельными задачами.

### Матрица прав

| Операция | reader | operator | admin |
|---|---|---|---|
| Исторические результаты в разрешённом scope | Да | Да | Да |
| Читать свои поля, настройки подписок, задачи | Да | Да | Да |
| Создавать/изменять/удалять свои поля | Нет | Да | Да |
| Сохранять неактивные настройки своих подписок | Нет | Да | Да |
| Удалять свои завершённые задачи | Нет | Да | Да |
| Читать справочник своей организации | Да | Да | Да |
| Менять справочник своей организации | Нет | Нет | Да |
| Управлять пользователями своей организации | Нет | Нет | Да |
| Читать журнал identity своей организации | Нет | Нет | Да |
| Читать чужие поля/подписки/jobs, включая свою организацию | Нет | Нет | Нет |
| Создавать прогнозы, запускать hindcast/refresh, активировать рассылку | Нет | Нет | Нет |

Администратор может создать пользователя и изменить его роль/активность, но не получает HTTP-команды для подмены его пароля, организации или владельца ресурсов. Сам пользователь меняет пароль с проверкой текущего. Нельзя деактивировать или понизить последнего активного администратора организации. Проверка сериализована блокировкой строки организации в PostgreSQL и проверена конкурентным тестом.

Авторизация выполняется и в ASGI-политике, и в слое хранения. Операции записи повторно проверяют актуальную сессию и роль внутри транзакции после получения блокировки организации. Уже начавшийся запрос чтения может закончиться; отзыв не объявлен отменой уже переданных ответов.

## Сессии, CSRF и ограничения входа

- `POST /api/auth/login` принимает только JSON с именем и паролем и точным `Origin`, заданным в `AGROCAST_PUBLIC_ORIGIN`. Публичной регистрации нет.
- Для других POST/PUT/PATCH/DELETE требуются и тот же `Origin`, и `X-CSRF-Token` текущей сессии. Токен получается при входе или через `GET /api/auth/me`; токен другой сессии не подходит.
- Cookies и пароли не сохраняются в localStorage/sessionStorage и не встраиваются в HTML. Frontend держит только CSRF-токен в памяти страницы. HTTP Basic из T01 больше не работает.
- Нет разрешающей cross-origin CORS-политики. Подмена `Host`/forwarded identity не меняет допустимый Origin или права.
- Счётчик попыток в БД ограничивает вход пятью попытками на имя в минуту и 30 на экземпляр identity-хранилища в минуту. Лимиты общие для API-реплик, а не process-local. Ответ — 429 с `Retry-After`.
- Одновременно хешируют/проверяют пароль не более двух потоков процесса; при перегрузке — 503 с `Retry-After`. Это ограничение памяти/CPU, не измеренный SLO.
- Для изменяющих запросов действует предел тела 16 KiB, включая chunked input, до запуска обработчика. Диагностика 422 не возвращает `input`/`ctx` с паролем.
- Недоступность PostgreSQL или некорректная схема закрывают доступ с 503; fallback на общий пароль или SQLite отсутствует.

Лимиты для закрытого пилота консервативны. Они не заменяют ingress rate limiting и анти-DDoS из эксплуатационных задач. `__Host-`/Secure cookie рассчитан на **HTTPS**. Во встроенном preview браузер может ограничивать сторонние cookies; для проверки входа открывать HTTPS preview отдельной вкладкой и задавать его точный Origin.

## Хранилище и миграция

Реализована минимальная часть зависимости T05, необходимая для T02:

- PostgreSQL + SQLAlchemy, миграции Alembic `0001_identity` и `0002_crop_revision`.
- Таблицы `organizations`, `users`, `sessions`, `login_limits`, `identity_events`, `fields`, `crops`, `subscriptions`, `jobs`.
- UUID, уникальность пользователей, ограничения ролей/статусов, владельцев и межтабличных связей.
- Миграция повторно применима без потери записей; metadata/schema сопоставлены на SQLite и PostgreSQL. Установка миграций PostgreSQL защищена advisory lock.
- Запуск API сам не создаёт таблицы. Compose выполняет отдельный migration job до API.
- SQLite используется только явной инъекцией engine в тестах. `IdentitySettings.from_environment()` допускает для HTTP deployment только `postgresql+psycopg`.

Старые `crops.db`, registry SQLite, process-local `JOBS` и поля в localStorage **не импортируются автоматически**: в них нет надёжной принадлежности пользователю. Ничего не удалено и не назначено первому вошедшему. Перенос требует backup, явного сопоставления owner/organization и проверки результата в T05. Сохранённые jobs в новом SQL-контуре читаются только владельцем; создание/исполнение job по HTTP пока запрещено. Удаление queued/running job запрещено до корректной отмены в T06.

Подписки сейчас только предпочтения: `active=false`, без подключения к старому autopilot и без отправки. Поле выбирается из собственных полей, `start_month` сохраняется. Удаление поля удаляет только связанные с ним настройки подписок. Режим/горизонт предпочтения не означают разрешение выпустить прогноз.

Остаются открытыми полный T05, очереди/outbox T06, bundle/backup/restore, least-privilege deployment и наблюдаемость. Наличие тома PostgreSQL не является проверкой восстановления. `downgrade` начальной миграции удаляет новые таблицы; его нельзя использовать как бездумный production rollback.

## Настройка

### Контролируемый Compose-пилот

Команды из корня репозитория, с собственным доменом. Docker/HTTPS startup этих файлов в sandbox не выполнялся.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r AgroCast/requirements-test.txt
cd AgroCast
../.venv/bin/python -m agrocast.identity.cli init-secrets --directory ../deploy/secrets --database-host database
cd ..
export AGROCAST_DOMAIN=agrocast.example.com
export AGROCAST_DATABASE_URL_FILE="$PWD/deploy/secrets/database_url"
export AGROCAST_DB_PASSWORD_FILE="$PWD/deploy/secrets/database_password"
docker compose -f deploy/docker-compose.yml config
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml run --rm --no-deps app python -m agrocast.identity.cli bootstrap --organization "Пилотное хозяйство" --username admin
```

CLI запросит пароль администратора дважды без отображения. Файлы секретов создаются эксклюзивно с правами 0600 и не перезаписываются. Содержимое не выводится. `deploy/secrets` исключён из Git и Docker context. Не передавать пароли аргументами команд, в URL браузера, в чате или в логах.

Сервисы: `database` → `migrate` → `app` → `caddy`. Наружу публикуются только 80/443 Caddy, не 8501 API и не 5432 PostgreSQL. API самостоятельно проверяет сессию, даже при прямом обращении к Uvicorn. Альтернативный `serve/api.py` по-прежнему отвечает 410. Старые переменные `AGROCAST_PILOT_USER`/`AGROCAST_PILOT_SECRET_FILE` не предоставляют доступ.

### Уже имеющийся PostgreSQL

Сохранить DSN `postgresql+psycopg://...` в защищённом внешнем файле, указать его путь, а не значение в env. Для удалённого PostgreSQL оператор должен настроить TLS с проверкой сертификата, сеть, отдельные права и backup. Для production ещё требуется проверка/разделение прав DB runtime и мигратора; текущий Compose — пилотная конфигурация.

```bash
export AGROCAST_DATABASE_URL_FILE=/secure/location/agrocast_database_url
export AGROCAST_PUBLIC_ORIGIN=https://agrocast.example.com
cd AgroCast
../.venv/bin/python -m agrocast.identity.cli migrate
../.venv/bin/python -m agrocast.identity.cli bootstrap --organization "Пилотное хозяйство" --username admin
../.venv/bin/python -m uvicorn agrocast.serve.product:app --host 0.0.0.0 --port 8501
```

Последнюю команду использовать только за настроенным HTTPS proxy в ограниченной сети. Origin — точное значение браузера без пути и завершающего `/`, не wildcard. Допустимый `AGROCAST_SESSION_SECONDS` — 300–86400, по умолчанию 28800. После изменения конфигурации или применения миграций к ранее неготовому сервису перезапустить API.

## HTTP-контракт T02

| Маршрут | Назначение |
|---|---|
| `GET /login`, `/login.js`, `/pilot.css` | Публичный экран входа и его assets |
| `POST /api/auth/login` | Личный вход; JSON, Origin; создаёт Secure cookie |
| `GET /api/auth/me` | Текущий пользователь и CSRF-токен |
| `POST /api/auth/logout` | Отзыв текущей сессии |
| `POST /api/auth/password` | Смена своего пароля и отзыв всех своих сессий |
| `GET/POST /api/admin/users`, `PATCH /api/admin/users/{uuid}` | Аккаунты своей организации, только admin |
| `GET /api/admin/events` | Журнал своей организации, только admin |
| `GET/POST /api/fields`, `GET/PUT/DELETE /api/fields/{uuid}` | Собственные поля |
| `GET/POST /api/subscriptions`, `GET/PUT/DELETE /api/subscriptions/{uuid}` | Свои неактивные настройки подписок |
| `GET/POST /api/crops`, `GET/PUT/DELETE /api/crops/{uuid}` | Справочник своей организации, запись только admin |
| `GET /api/jobs`, `GET/DELETE /api/jobs/{uuid}` | Собственные записи jobs; удаление только завершённых |
| `GET /api/job/{uuid}` | Защищённый alias чтения своей job; без fallback на старый `JOBS` |

Списки ограничены `limit=1…100`. Запись имеет серверные `id`, `owner_id`, `organization_id`, timestamps и объект `data`. PUT принимает полное содержимое `data`, не метаданные владения. Сорт адресуется UUID, не именем, и имеет атомарно увеличиваемую `revision`. T04 добавил строгие параметры/ответы, расширил неактивные настройки подписки и запрещает неизвестные query-поля; [текущий контракт](API_CACHE.md). Анонимный API-запрос — 401, недостаточная роль — 403, чужой/несуществующий ресурс — одинаковый 404; HTML-навигация без сессии перенаправляется на `/login`. Неизвестные/отключённые операции — 403 по default-deny политике.

Пример клиента для operator/admin; пароль вводится локально, токены не печатаются:

```python
import getpass
import os
import requests

origin = os.environ["AGROCAST_PUBLIC_ORIGIN"]
client = requests.Session()
login = client.post(
    origin + "/api/auth/login",
    headers={"Origin": origin},
    json={"username": input("Пользователь: "), "password": getpass.getpass()},
    timeout=15,
)
login.raise_for_status()
client.headers.update({"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]})
created = client.post(
    origin + "/api/fields",
    json={"name": "Северное", "point_id": "P01", "area_ha": 120},
    timeout=15,
)
created.raise_for_status()
print(created.json()["field"]["id"])
client.post(origin + "/api/auth/logout", timeout=15).raise_for_status()
```

## Проверки и границы результата

Выполнены HTTP-проверки входа/выхода, Secure cookie, Origin/CSRF, подмены заголовков, ролей, tenant/owner isolation, конфликтов, отзыва доступа, перезапуска API, persistence/rate limits, SQL foreign keys, миграций и конкурентной защиты последнего admin. Проверены также отключённые научные возможности T01 для **всех трёх ролей**.

[Точные результаты и версии](PROGRESS.md). В T03 отдельно выполнены браузерные DOM/CSP/auth-проверки Chromium. Реальный Compose/TLS, backup/restore, production PostgreSQL 17 image и внешние проверки безопасности ещё не выполнялись. Новый CI job PostgreSQL добавлен, но удалённый запуск GitHub Actions в этом шаге не заявляется.
