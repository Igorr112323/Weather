import argparse
import json

import pandas as pd

from agrocast.core.config import Config
from agrocast.core.settings import RuntimeSettings, ConfigurationError


def _load_config(data_dir=None, world_dir=None):
    return RuntimeSettings.from_environment().with_paths(world_dir, data_dir).compute_config()


def main(argv=None):
    p = argparse.ArgumentParser(prog="agrocast")
    p.add_argument("--state-dir", "--data-dir", dest="data_dir", default=None)
    p.add_argument("--world-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-synthetic")

    sub.add_parser("update-open")

    sp = sub.add_parser("update-real")
    sp.add_argument("--start-year", type=int, default=None)
    sp.add_argument("--end-year", type=int, default=None)

    sp = sub.add_parser("update-station")
    sp.add_argument("--station", required=True)
    sp.add_argument("--start", default="1991-01-01")

    sp = sub.add_parser("backtest")
    sp.add_argument("--vars", default="t2m,tp")
    sp.add_argument("--months", default=None)
    sp.add_argument("--leads", default=None)
    sp.add_argument("--start-year", type=int, default=None)
    sp.add_argument("--end-year", type=int, default=None)
    sp.add_argument("--lat", type=float, default=None)
    sp.add_argument("--lon", type=float, default=None)
    sp.add_argument("--mode", default="monthly", choices=["monthly", "seasonal"])
    sp.add_argument("--season-len", type=int, default=3)

    sp = sub.add_parser("forecast")
    sp.add_argument("--lat", type=float, required=True)
    sp.add_argument("--lon", type=float, required=True)
    sp.add_argument("--month", type=int, required=True)
    sp.add_argument("--year", type=int, default=None)
    sp.add_argument("--horizon", type=int, default=3)
    sp.add_argument("--vars", default="t2m,tp")
    sp.add_argument("--out", default=None)
    sp.add_argument("--mode", default="monthly", choices=["monthly", "seasonal"])
    sp.add_argument("--season-len", type=int, default=3)

    sp = sub.add_parser("serve", help="Закрытый пилот с личными аккаунтами; настройки AGROCAST_*")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8000)

    sub.add_parser("autopilot")
    sub.add_parser("verify")
    sub.add_parser("settings")
    sub.add_parser("skill")

    args = p.parse_args(argv)
    try:
        settings = RuntimeSettings.from_environment().with_paths(args.world_dir, args.data_dir)
        if args.cmd not in {"init-synthetic", "serve", "settings"}:
            cfg = settings.compute_config()
    except ConfigurationError as error:
        raise SystemExit(str(error)) from None
    if args.cmd == "settings":
        print(json.dumps(settings.public_snapshot(), ensure_ascii=False, sort_keys=True))
        return
    if args.cmd == "serve":
        import uvicorn
        from agrocast.serve.product import create_app

        uvicorn.run(create_app(settings=settings), host=args.host, port=args.port)
        return

    if args.cmd == "init-synthetic":
        cfg = Config(data_dir=str(settings.state_dir / "compute"), runtime_dir=str(settings.state_dir), **settings.numeric_overrides())
    settings.prepare_state()
    cfg.save()

    if args.cmd == "init-synthetic":
        from agrocast.ingest.synthetic import build_synthetic

        build_synthetic(cfg)
        print("synthetic datasets ready")

    elif args.cmd == "update-open":
        from agrocast.ingest.openobs import update_all

        print(json.dumps(update_all(cfg), default=str))

    elif args.cmd == "update-real":
        from agrocast.autopilot.cycle import update_data

        print(json.dumps(update_data(cfg, era5_start=args.start_year), default=str))

    elif args.cmd == "update-station":
        from agrocast.ingest.stations import update_station

        print(json.dumps(update_station(cfg, args.station, start_date=args.start), default=str))

    elif args.cmd == "backtest":
        from agrocast.backtest.engine import run_backtest, skill_summary

        months = [int(x) for x in args.months.split(",")] if args.months else None
        leads = [int(x) for x in args.leads.split(",")] if args.leads else None
        years = None
        if args.start_year or args.end_year:
            years = range(args.start_year or cfg.backtest_start, (args.end_year or pd.Timestamp.now().year) + 1)
        rec = run_backtest(
            cfg,
            variables=tuple(args.vars.split(",")),
            start_months=months,
            leads=leads,
            years=years,
            lat=args.lat,
            lon=args.lon,
            mode=args.mode,
            season_len=args.season_len,
        )
        if rec is not None and len(rec):
            print(skill_summary(rec).to_string(index=False))

    elif args.cmd == "forecast":
        from agrocast.forecast.orchestrator import forecast_point
        from agrocast.core.timeutils import next_occurrence, month_period

        start = month_period(args.year, args.month) if args.year else next_occurrence(args.month)
        fc = forecast_point(cfg, args.lat, args.lon, start=start, horizon=args.horizon, variables=tuple(args.vars.split(",")), mode=args.mode, season_len=args.season_len)
        text = json.dumps(fc, ensure_ascii=False, indent=2, default=str)
        if args.out:
            settings.writable_path(args.out).write_text(text)
        else:
            print(text)

    elif args.cmd == "autopilot":
        from agrocast.autopilot.cycle import schedule

        schedule(cfg)

    elif args.cmd == "verify":
        from agrocast.autopilot.cycle import verify_past

        print(json.dumps({"scores_saved": verify_past(cfg)}))

    elif args.cmd == "skill":
        from agrocast.backtest.engine import load_skill_map, load_records, skill_summary

        for mode in ["monthly", "seasonal"]:
            smap = load_skill_map(cfg, mode)
            if smap is not None:
                print(f"[{mode}]")
                print(smap.to_string(index=False))
            rec = load_records(cfg, mode)
            if rec is not None and len(rec):
                print(skill_summary(rec).to_string(index=False))


if __name__ == "__main__":
    main()
