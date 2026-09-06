from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agrocast.core.settings import RuntimeSettings


def main():
    from agrocast.serve.region import build_field
    from agrocast.store.results import Releases

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--release-manifest", default=None)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    settings = RuntimeSettings.from_environment()
    t0 = time.time()
    fp = build_field(args.start, settings.world_dir, settings.state_dir, workers=args.workers, releases=Releases.from_file(args.release_manifest),
                     log=lambda m: print(f"[krig_demo] {time.strftime('%H:%M:%S')} {m}", flush=True))
    print(f"[krig_demo] {time.strftime('%H:%M:%S')} готово за {time.time() - t0:.0f}s → {fp}")
    return fp


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
