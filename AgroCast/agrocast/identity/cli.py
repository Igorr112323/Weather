import argparse
import getpass
import json
import os
import secrets
from pathlib import Path

from sqlalchemy.engine import URL

from agrocast.identity.credentials import IdentityError
from agrocast.identity.database import IdentitySettings, migrate
from agrocast.identity.service import IdentityService


def init_secrets(directory, host):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    password = secrets.token_urlsafe(32)
    url = URL.create("postgresql+psycopg", username="agrocast", password=password, host=host, database="agrocast")
    values = {"database_password": password, "database_url": url.render_as_string(hide_password=False)}
    created = []
    try:
        for name, value in values.items():
            path = directory / name
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created.append(path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    print("Созданы database_password и database_url; содержимое не выводится")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agrocast-identity")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("init-secrets")
    setup.add_argument("--directory", required=True)
    setup.add_argument("--database-host", default="database")
    commands.add_parser("migrate")
    commands.add_parser("check")
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--organization", required=True)
    bootstrap.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    engine = None
    try:
        if args.command == "init-secrets":
            init_secrets(args.directory, args.database_host)
            return
        settings = IdentitySettings.from_environment()
        if settings is None:
            raise ValueError("Missing configuration")
        engine = settings.engine()
        if args.command == "migrate":
            migrate(engine)
            print("Миграции identity применены")
            return
        identity = IdentityService(engine, settings.public_origin, settings.session_seconds)
        if args.command == "check":
            print("Схема identity готова")
            return
        password = getpass.getpass("Пароль нового администратора: ")
        confirmation = getpass.getpass("Повторите пароль: ")
        if password != confirmation:
            raise IdentityError("password_confirmation_failed", 422)
        result = identity.bootstrap(args.organization, args.username, password)
        print(json.dumps(result, ensure_ascii=False))
    except IdentityError as error:
        raise SystemExit(error.code) from None
    except Exception:
        raise SystemExit("Операция не выполнена: проверьте конфигурацию, PostgreSQL, миграции и существование файлов") from None
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
