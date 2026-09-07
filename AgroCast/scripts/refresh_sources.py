import argparse
import json
import sys
from pathlib import Path


def _os_lock(handle):
    import fcntl

    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(handle):
    import fcntl

    fcntl.flock(handle, fcntl.LOCK_UN)


def _win_lock(handle):
    import msvcrt

    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _win_unlock(handle):
    import msvcrt

    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


import os as _os

if _os.name == "nt":
    _lock = _win_lock
    _unlock = _win_unlock
else:
    _lock = _os_lock
    _unlock = _os_unlock


def acquire_lock(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    handle = open(state_dir / "refresh.lock", "a+")
    try:
        _lock(handle)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv=None):
    parser = argparse.ArgumentParser(prog="refresh_sources")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--world", type=Path)
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--soil", action="store_true")
    args = parser.parse_args(argv)
    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon must be given together")
    if (args.lat is not None or args.soil) and args.world is None:
        parser.error("--world is required for point or soil refresh")
    handle = acquire_lock(args.state)
    if handle is None:
        print(json.dumps({"ok": False, "error": "another refresh is already running"}))
        return 4
    try:
        from agrocast.ingest.openobs import fetch_cpc_daily, fetch_soil
        from agrocast.serve.pipeline import point_config, world_config

        if args.lat is not None:
            cfg, _ = point_config(args.world, args.state, args.lat, args.lon)
        else:
            cfg = world_config(args.world, args.state)
        result = {"daily": fetch_cpc_daily(cfg)}
        if args.lat is not None or args.soil:
            result["soil"] = fetch_soil(cfg)
        print(json.dumps({"ok": True, **result}, default=str))
        return 0
    finally:
        _unlock(handle)
        handle.close()


if __name__ == "__main__":
    sys.exit(main())
