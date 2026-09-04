from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

WORLD = str(BASE / "world")
DATA_ROOT = str(BASE / "data")


def main():
    from agrocast.serve.region import build_field, field_path

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-10")
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    t0 = time.time()
    fp = build_field(args.start, WORLD, DATA_ROOT, workers=args.workers,
                     log=lambda m: print(f"[krig_demo] {time.strftime('%H:%M:%S')} {m}", flush=True))
    print(f"[krig_demo] {time.strftime('%H:%M:%S')} готово за {time.time() - t0:.0f}s → {fp}")
    return fp


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
