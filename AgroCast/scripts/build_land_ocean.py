import sys
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from agrocast.serve.pipeline import world_config
from agrocast.features.dataset import PointDataset
from agrocast.phys.surface import SoilSnowColumn, batch_integrate, st_to_state
from agrocast.phys.ocean import TwoLayerSST, DelayedSST, box_series, BOXES

STATE_COLS = ["t1", "t2", "t3", "t4", "w1", "w2", "w3", "w4", "swe", "snowcov",
              "frozen_cm", "thflux", "fztw", "swdef", "adef", "laifrac", "melt_idx"]
FC_COLS = ["t1", "w1", "swe", "snowcov", "thflux", "adef", "laifrac"]
N_MEMBERS = 20
SIG_T = 0.8
SIG_TP = 0.6
LEADS = [1, 2, 3, 4, 5, 6]


def stable_seed(s):
    return zlib.crc32(s.encode())


def ice_fraction(t_air, tp):
    f = np.clip((1.0 - t_air) / 4.0, 0.0, 1.0)
    return np.where(tp > 0.0, f, 0.0)


def build_surface(pt, soil_periods, swvl, snow):
    d = pt.daily().copy()
    d["t2m"] = d["t2m"].interpolate(limit_direction="both")
    d["tp"] = d["tp"].fillna(0.0)
    idx = pd.to_datetime(d.index)
    t = d["t2m"].to_numpy(float)
    p = d["tp"].to_numpy(float)
    doys = idx.dayofyear.to_numpy()
    lo = pd.Timestamp("1991-01-01")
    hi = pd.Timestamp("2020-12-31")
    mask = (idx >= lo) & (idx <= hi)
    doy = doys[mask]
    ct = t[mask].astype(float)
    cp = p[mask].astype(float)
    clim_t = np.array([
        float(np.nanmean(ct[doy == n])) if np.any(doy == n) and np.isfinite(np.nanmean(ct[doy == n]))
        else float(np.nanmean(ct)) for n in range(1, 367)
    ])
    clim_p = np.array([
        float(np.nanmean(cp[doy == n])) if np.any(doy == n) and np.isfinite(np.nanmean(cp[doy == n]))
        else float(np.nanmean(cp)) for n in range(1, 367)
    ])
    soil_pos = {k: i for i, k in enumerate(soil_periods)}
    col = SoilSnowColumn(45.0)
    monthly_state = {}
    fz_ref = 0.0
    for i in range(len(t)):
        col.step_day(t[i], p[i], float(ice_fraction(t[i], p[i])), int(doys[i]))
        pm = idx[i].to_period("M")
        if idx[i].day == pm.days_in_month:
            pos = soil_pos.get(pm)
            if pos is not None:
                col.assimilate_swvl(swvl[pos])
                col.assimilate_snow(snow[pos])
            s = col.state()
            s["fztw"] = float(col.fz_count - fz_ref)
            fz_ref = float(col.fz_count)
            monthly_state[pm] = s
    return idx, monthly_state, clim_t, clim_p


def predict_states(monthly_state, clim_t, clim_p, issues):
    out = {}
    for issue in issues:
        st0 = monthly_state.get(issue)
        if st0 is None:
            continue
        cols = [SoilSnowColumn(45.0) for _ in range(N_MEMBERS)]
        for c in cols:
            for j, k in enumerate(["t1", "t2", "t3", "t4"]):
                c.T[j] = st0[k] + 273.15
            for j, k in enumerate(["w1", "w2", "w3", "w4"]):
                c.W[j] = st0[k]
            c.swe = st0["swe"]
            c.et_c = st0["et_c"]
            c.fz_count = int(st0["fztw"])
        for h in LEADS:
            tgt = issue + h
            start = pd.Timestamp((issue + 1).to_timestamp())
            end = pd.Timestamp(tgt.to_timestamp()) + pd.offsets.MonthEnd(1)
            days = (end - start).days
            doys = (np.arange(start.dayofyear, start.dayofyear + days) - 1) % 365 + 1
            rng = np.random.default_rng(stable_seed(f"{issue}-{h}"))
            ta = np.tile(clim_t[doys], (N_MEMBERS, 1)).T + rng.normal(0.0, SIG_T, (days, N_MEMBERS))
            cp_ = np.tile(clim_p[doys], (N_MEMBERS, 1)).T
            tp = np.where(cp_ > 0, cp_ * np.exp(rng.normal(0.0, SIG_TP, cp_.shape)), 0.0)
            ice = ice_fraction(ta, tp)
            st = batch_integrate(cols, ta, tp, ice, doys)
            feats = [st_to_state(st, i) for i in range(N_MEMBERS)]
            mean = {k: float(np.mean([f[k] for f in feats])) for k in FC_COLS + ["fztw", "swdef"]}
            spread = {k: float(np.std([f[k] for f in feats])) for k in FC_COLS + ["fztw", "swdef"]}
            out[(issue, h)] = {"mean": mean, "spread": spread}
    return out


def main():
    wc = world_config()
    pt = PointDataset(wc, 45.0, 39.5, wc.zarr_store())
    soil = pt.soil_monthly()
    soil_periods = list(pd.period_range("1948-01", "2026-08", freq="M"))
    swvl = soil["swvl"].to_numpy(float)
    snow = soil["snow"].to_numpy(float)
    print("модель поверхности 1979-н.")
    idx, monthly_state, clim_t, clim_p = build_surface(pt, soil_periods, swvl, snow)
    issues = list(pd.period_range("1984-01", "2026-02", freq="M"))
    print(f"прогноз состояния: {len(issues)} issues x {len(LEADS)} leads x {N_MEMBERS} members.")
    preds = predict_states(monthly_state, clim_t, clim_p, issues)
    sst = wc.zarr_store().open("sst")["sst"]
    print("океан: fit 1981-2003 + forecast.")
    ocean = {}
    delayed = {}
    for name in BOXES:
        s = box_series(sst, *BOXES[name]).dropna()
        m = TwoLayerSST()
        d = DelayedSST(tau=18)
        mok = m.fit(s, fit_end=2003)
        dok = d.fit(s, fit_end=2003)
        if not mok and not dok:
            continue
        for issue in issues:
            if m.r is not None:
                f = m.forecast(s, issue, LEADS, n_members=N_MEMBERS, seed=stable_seed("ocean-" + name))
                if f is not None:
                    ocean[(name, issue)] = f
            if d.c1 is not None:
                f = d.forecast(s, issue, LEADS, n_members=N_MEMBERS, seed=stable_seed("delay-" + name))
                if f is not None:
                    delayed[(name, issue)] = f

    rows = {}
    for issue in issues:
        r = {}
        st0 = monthly_state.get(issue)
        if st0 is not None:
            for k in STATE_COLS:
                v = st0[k]
                if k == "melt_idx":
                    v = 180.0 if v is None or np.isnan(v) else min(float(v), 180.0)
                r["ls_" + k] = v
        for h in LEADS:
            pd_ = preds.get((issue, h))
            if pd_ is None:
                continue
            r[f"lsf_t1_f{h}"] = pd_["mean"]["t1"]
            r[f"lsf_w1_f{h}"] = pd_["mean"]["w1"]
            r[f"lsf_swe_f{h}"] = pd_["mean"]["swe"]
            if h == 3:
                for k in ["t1", "w1", "swe", "thflux", "swdef"]:
                    r[f"lss_sp_{k}_f3"] = pd_["spread"][k]
        for name in BOXES:
            f = ocean.get((name, issue))
            fd = delayed.get((name, issue))
            if f is None and fd is None:
                continue
            for h in (1, 3):
                if f is not None:
                    r[f"sstfc_{name}_f{h}"] = float(f["mean"][LEADS.index(h)])
            if fd is not None:
                r[f"sstfc_{name}_f6"] = float(fd["mean"][LEADS.index(6)])
            elif f is not None:
                r[f"sstfc_{name}_f6"] = float(f["mean"][LEADS.index(6)])
        rows[issue] = r
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "issue"
    out = wc.artifact_dir / "land_ocean_features.parquet"
    df.to_parquet(out)
    print("rows", len(df), "cols", len(df.columns))

    msw = []
    for k, st in monthly_state.items():
        pos = {kk: i for i, kk in enumerate(soil_periods)}.get(k)
        if pos is not None and k >= pd.Period("2000-01", "M") and np.isfinite(swvl[pos]):
            msw.append((st["w1"], swvl[pos]))
    a = np.array([x for x, _ in msw], float)
    b = np.array([y for _, y in msw], float)
    print(f"валидация w1 vs NCEP swvl: corr={np.corrcoef(a, b)[0, 1]:.3f} rmse={np.sqrt(((a - b) ** 2).mean()):.3f}")
    ms = []
    soil_pos2 = {kk: i for i, kk in enumerate(soil_periods)}
    for k, st in monthly_state.items():
        pos = soil_pos2.get(k)
        if pos is not None and k >= pd.Period("2000-01", "M") and np.isfinite(snow[pos]):
            ms.append((st["snowcov"], snow[pos]))
    c1 = np.array([x for x, _ in ms], float)
    c2 = np.array([y for _, y in ms], float)
    print(f"валидация snowcov vs NCEP snow: corr={np.corrcoef(c1, c2)[0, 1]:.3f}")

    for name in BOXES:
        s = box_series(sst, *BOXES[name]).dropna()
        s.index = pd.PeriodIndex(s.index, freq="M")
        base = s.groupby(s.index.month).transform("mean")
        a = (s - base).dropna()
        sk = []
        for h in (1, 3, 6):
            src = delayed if h == 6 else ocean
            acc, obs = [], []
            for y in range(2004, 2025):
                for mo in range(1, 13):
                    issue = pd.Period(f"{y}-{mo:02d}", "M")
                    f = src.get((name, issue))
                    v = a.get(issue + h, np.nan) if f is not None else np.nan
                    if f is not None and np.isfinite(v):
                        acc.append(f["mean"][LEADS.index(h)])
                        obs.append(v)
            if len(acc) > 30:
                sk.append(float(np.corrcoef(acc, obs)[0, 1]))
        print(f"океан {name}: corr lead1/3/6 = {' '.join(f'{x:.3f}' for x in sk)}")


if __name__ == "__main__":
    main()
