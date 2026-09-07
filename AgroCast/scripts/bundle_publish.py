import argparse
import json
import sys
from pathlib import Path

from agrocast.bundle.releases import (
    BundleError,
    publish,
    release_status,
    rollback,
    set_active,
    verify_release,
)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bundle_publish")
    parser.add_argument("--releases-root", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    p_pub = sub.add_parser("publish")
    p_pub.add_argument("source", type=Path)
    p_pub.add_argument("--no-activate", action="store_true")
    sub.add_parser("status")
    sub.add_parser("rollback")
    sub.add_parser("list")
    p_ver = sub.add_parser("verify")
    p_ver.add_argument("release_id")
    p_act = sub.add_parser("activate")
    p_act.add_argument("release_id")
    args = parser.parse_args(argv)
    root = args.releases_root
    if args.command == "publish":
        result = publish(args.source, root, activate=not args.no_activate)
    elif args.command == "status":
        result = release_status(root)
    elif args.command == "rollback":
        result = rollback(root)
    elif args.command == "verify":
        problems = verify_release(root / args.release_id, full=True)
        result = {"release_id": args.release_id, "ok": not problems, "problems": problems[:20]}
    elif args.command == "activate":
        result = set_active(root, args.release_id)
    else:
        result = {"releases": sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))} if root.is_dir() else {"releases": []}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) and "error" not in result else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BundleError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
