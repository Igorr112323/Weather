import argparse
from pathlib import Path

from agrocast.desktop.app import _bundle_digest, _verify_bundle_integrity


def main(argv=None):
    parser = argparse.ArgumentParser(description="Проверить набор данных в desktop-сборке")
    parser.add_argument("world_dir", type=Path)
    args = parser.parse_args(argv)

    world_dir = args.world_dir.resolve()
    marker = world_dir / "integrity.json"
    if not marker.is_file():
        raise SystemExit("desktop bundle marker is missing: %s" % marker)
    _verify_bundle_integrity(world_dir)
    print("desktop bundle integrity ok: %s" % _bundle_digest(world_dir)[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
