import argparse
import json
from pathlib import Path

from agrocast.core.jsoncodec import strict_json
from agrocast.core.settings import ConfigurationError, RuntimeSettings
from agrocast.state.backup import StateError, backup_database, restore_database
from agrocast.state.legacy import import_snapshot
from agrocast.state.filesystem import backup_runtime, restore_runtime
from agrocast.state.snapshot import create_snapshot, read_snapshot
from agrocast.store.atomic import write_json


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agrocast-state")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("settings")
    snapshot = commands.add_parser("snapshot-legacy")
    snapshot.add_argument("--source", required=True)
    snapshot.add_argument("--output", required=True)
    snapshot.add_argument("--fields-json")
    snapshot.add_argument("--jobs-json")
    snapshot.add_argument("--no-in-memory-jobs", action="store_true")
    verify = commands.add_parser("verify-snapshot")
    verify.add_argument("--snapshot", required=True)
    jobs = commands.add_parser("export-offline-jobs")
    jobs.add_argument("--output", required=True)
    apply = commands.add_parser("import-legacy")
    apply.add_argument("--snapshot", required=True)
    apply.add_argument("--mapping", required=True)
    apply.add_argument("--namespace", required=True)
    apply.add_argument("--backup", required=True)
    apply.add_argument("--report", required=True)
    apply.add_argument("--maintenance-confirmed", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--output", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--empty-target-confirmed", action="store_true")
    runtime_backup = commands.add_parser("backup-runtime")
    runtime_backup.add_argument("--output", required=True)
    runtime_backup.add_argument("--maintenance-confirmed", action="store_true")
    runtime_restore = commands.add_parser("restore-runtime")
    runtime_restore.add_argument("--backup", required=True)
    runtime_restore.add_argument("--empty-target-confirmed", action="store_true")
    args = parser.parse_args(argv)
    engine = None
    try:
        settings = RuntimeSettings.from_environment()
        for name in ("output", "backup", "report"):
            value = getattr(args, name, None)
            if value is not None and not (name == "backup" and args.command in {"restore", "restore-runtime"}):
                path = Path(value).resolve()
                if path == settings.world_dir or settings.world_dir in path.parents:
                    raise StateError("state operations cannot write into the read-only bundle")
        if args.command == "settings":
            result = settings.public_snapshot()
        elif args.command == "snapshot-legacy":
            result = create_snapshot(args.source, args.output, args.fields_json, args.jobs_json, args.no_in_memory_jobs)
        elif args.command == "verify-snapshot":
            value = read_snapshot(args.snapshot)
            result = {"verified": True, "counts": value["counts"], "checksum": value["checksum"]}
        elif args.command == "backup-runtime":
            if not args.maintenance_confirmed:
                raise StateError("runtime backup requires --maintenance-confirmed and all writers stopped")
            result = backup_runtime(settings, args.output)
        elif args.command == "restore-runtime":
            if not args.empty_target_confirmed:
                raise StateError("runtime restore requires --empty-target-confirmed and a stopped service")
            result = restore_runtime(settings, args.backup)
        elif args.command == "export-offline-jobs":
            records = [strict_json(path.read_bytes()) for path in sorted((settings.state_dir / "offline-jobs").glob("*.json"))]
            write_json(args.output, records, exclusive=True)
            result = {"jobs": len(records), "output": args.output, "ownership": "unassigned_offline"}
        else:
            options = settings.identity_settings()
            engine = options.engine()
            if args.command == "backup":
                result = backup_database(engine, args.output)
            elif args.command == "restore":
                if not args.empty_target_confirmed:
                    raise StateError("restore requires --empty-target-confirmed and a stopped service")
                result = restore_database(engine, args.backup)
            else:
                if not args.maintenance_confirmed:
                    raise StateError("legacy import requires --maintenance-confirmed and all writers stopped")
                if Path(args.report).exists():
                    raise StateError("migration report already exists")
                backup_database(engine, args.backup)
                result = import_snapshot(engine, args.snapshot, args.mapping, args.namespace)
                write_json(args.report, result, exclusive=True)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (StateError, ConfigurationError) as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("State operation failed: check files, PostgreSQL and schema; no credentials are shown") from None
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
