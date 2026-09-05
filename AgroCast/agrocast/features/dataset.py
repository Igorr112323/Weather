import re

import numpy as np
import pandas as pd

from agrocast.features.lim import lim_forecast_frame

from agrocast.core.geo import snap
from agrocast.features.climatology import monthly_from_daily, standardize_monthly, past_monthly_anom, past_standardize, seasonal_series
from agrocast.features.teleconnections import indices_from_store, IDX_COLS, _box_series


def _prep_lon(ds):
    ds = ds.sortby("lat")
    return ds.assign_coords(lon=(ds.lon.values % 360.0)).sortby("lon")


class PointDataset:
    def __init__(self, config, lat, lon, store=None):
        self.config = config
        self.lat = float(lat)
        self.lon = float(lon)
        self.store = store or config.zarr_store()
        self._daily = None
        self._daily_cal = None
        self._monthly = None
        self._monthly_raw = None
        self._fcalib = None
        self._indices = None
        self._pcs = None
        self._pf = None
        self._seas_raw = {}
        self._seas_std = {}
        self._ospr = {}
        self.grid_lat = None
        self.grid_lon = None

    def raw_daily(self):
        if self._daily is None:
            ds = self.store.open("daily_region")
            lats, lons = ds.lat.values, ds.lon.values
            gl, go = snap(self.lat, self.lon, lats, lons)
            cols = [c for c in ["t2m", "tp", "swvl", "snow"] if c in ds.data_vars]

            def cell_series(la, lo):
                frames = [ds[c].sel(lat=la, lon=lo).to_series().rename(c) for c in cols]
                return pd.concat(frames, axis=1).sort_index()

            def usable(d2):
                return len(d2) > 0 and float(d2.notna().mean().min()) > 0.9

            df = cell_series(gl, go)
            if not usable(df):
                ilat = int(np.argmin(np.abs(lats - gl)))
                ilon = int(np.argmin(np.abs(((lons - self.lon + 180) % 360) - 180)))
                for ring in range(1, 4):
                    cand = []
                    for dla in range(-ring, ring + 1):
                        for dlo in range(-ring, ring + 1):
                            if max(abs(dla), abs(dlo)) != ring:
                                continue
                            ia, io = ilat + dla, ilon + dlo
                            if 0 <= ia < len(lats) and 0 <= io < len(lons):
                                cand.append((dla * dla + dlo * dlo, float(lats[ia]), float(lons[io])))
                    cand.sort()
                    for _, la, lo in cand:
                        d2 = cell_series(la, lo)
                        if usable(d2):
                            df, gl, go = d2, la, lo
                            break
                    else:
                        continue
                    break
            self.grid_lat, self.grid_lon = float(gl), float(go)
            self._daily = df
        return self._daily

    def _fixed_calibration(self):
        if self._fcalib is not None:
            return self._fcalib
        self._fcalib = False
        if not getattr(self.config, "station_calibrated_targets", False):
            return self._fcalib
        try:
            from agrocast.ingest.stations import nearest_station, station_monthly_cached

            st, dist = nearest_station(self.lat, self.lon)
            if dist > 250.0:
                return self._fcalib
            sm = station_monthly_cached(self.config, st["id"])
            if sm is None:
                return self._fcalib
            gm = self.raw_monthly()
            joined = gm.join(sm, lsuffix="_g", rsuffix="_s", how="inner").dropna()
            joined = joined[joined.index.year <= self.config.calib_end_year]
            out = {"station": st["name"], "station_id": st["id"], "distance_km": round(float(dist), 1), "n_months": int(len(joined))}
            if len(joined) < 60:
                return self._fcalib
            if "t2m_g" in joined.columns and "t2m_s" in joined.columns:
                out["t2m_bias"] = float((joined["t2m_s"] - joined["t2m_g"]).mean())
            if "tp_g" in joined.columns and "tp_s" in joined.columns:
                out["tp_ratio"] = float(joined["tp_s"].sum() / max(joined["tp_g"].sum(), 1e-9))
            if "t2m_bias" not in out and "tp_ratio" not in out:
                return self._fcalib
            self._fcalib = out
        except Exception:
            self._fcalib = False
        return self._fcalib

    @property
    def fixed_calibration_active(self):
        return bool(self._fixed_calibration())

    def daily(self):
        calib = self._fixed_calibration()
        if calib is False:
            return self.raw_daily()
        if self._daily_cal is None:
            df = self.raw_daily().copy()
            if "t2m_bias" in calib and "t2m" in df.columns:
                df["t2m"] = df["t2m"] + calib["t2m_bias"]
            if "tp_ratio" in calib and "tp" in df.columns:
                df["tp"] = df["tp"] * calib["tp_ratio"]
            self._daily_cal = df
        return self._daily_cal

    def raw_monthly(self):
        if self._monthly_raw is None:
            self._monthly_raw = monthly_from_daily(self.raw_daily())
        return self._monthly_raw

    def monthly(self):
        if self._monthly is None:
            self._monthly = monthly_from_daily(self.daily())
        return self._monthly

    def soil_monthly(self):
        if not self.store.exists("soil_monthly"):
            return None
        ds = self.store.open("soil_monthly")
        lats, lons = ds.lat.values, ds.lon.values
        gl, go = snap(self.lat, self.lon, lats, lons)
        cols = [c for c in ["swvl", "snow"] if c in ds.data_vars]
        if not cols:
            return None

        def cell_df(la, lo):
            frames = [ds[c].sel(lat=la, lon=lo).to_series().rename(c) for c in cols]
            return pd.concat(frames, axis=1)

        def usable(d2):
            if len(d2) == 0:
                return False
            for c in d2.columns:
                first = d2[c].first_valid_index()
                if first is None:
                    return False
                tail = d2[c].loc[first:]
                if len(tail) == 0 or float(tail.notna().mean()) <= 0.9:
                    return False
            return True

        df = cell_df(gl, go)
        if not usable(df):
            ilat = int(np.argmin(np.abs(lats - gl)))
            ilon = int(np.argmin(np.abs(((lons - self.lon + 180) % 360) - 180)))
            for ring in range(1, 4):
                cand = []
                for dla in range(-ring, ring + 1):
                    for dlo in range(-ring, ring + 1):
                        if max(abs(dla), abs(dlo)) != ring:
                            continue
                        ia, io = ilat + dla, ilon + dlo
                        if 0 <= ia < len(lats) and 0 <= io < len(lons):
                            cand.append((dla * dla + dlo * dlo, float(lats[ia]), float(lons[io])))
                cand.sort()
                for _, la, lo in cand:
                    d2 = cell_df(la, lo)
                    if usable(d2):
                        df = d2
                        break
                else:
                    continue
                break
        if not usable(df):
            return None
        p = pd.PeriodIndex(df.index, freq="M")
        return df.groupby(p).mean()

    def land_ocean_frame(self):
        p = self.config.source_artifact("land_ocean_features.parquet")
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        df.index = pd.PeriodIndex(df.index, freq="M")
        keep = [c for c in df.columns if float(np.nanstd(df[c].to_numpy(float))) > 1e-9]
        preset = self.config.phys_preset
        if preset == "state":
            keep = [c for c in keep if c.startswith("ls_")]
        elif preset == "land_d6":
            keep = [c for c in keep if c.startswith(("ls_", "lsf_", "lss_")) or (c.startswith("sstfc_") and c.endswith("_f6"))]
        else:
            keep = [c for c in keep if c.startswith(("ls_", "lsf_", "lss_")) or c.startswith("sstfc_")]
        return df[keep]

    def indices(self):
        if self._indices is None:
            self._indices = indices_from_store(self.config, self.store)
        return self._indices

    def sst_pcs(self):
        if self._pcs is not None:
            return self._pcs
        cfg = self.config
        sst = _prep_lon(self.store.open("sst"))["sst"]
        base_slice = slice(f"{cfg.base_start}-01-01", f"{cfg.base_end}-12-31")
        clim = sst.sel(time=base_slice).groupby("time.month").mean("time")
        anom = sst.groupby("time.month") - clim
        w = np.sqrt(np.clip(np.cos(np.radians(anom.lat.values)), 0.0, 1.0))
        Xb = anom.sel(time=base_slice).values.astype(float) * w[None, :, None]
        T, La, Lo = Xb.shape
        flat_b = Xb.reshape(T, -1)
        ok = np.isfinite(flat_b).all(axis=0)
        F = flat_b[:, ok]
        Fc = F - F.mean(axis=0, keepdims=True)
        U, S, Vt = np.linalg.svd(Fc, full_matrices=False)
        n = min(cfg.n_pcs, Vt.shape[0])
        eofs = Vt[:n]
        roll = anom.rolling(time=3, min_periods=3).mean()
        Y = roll.values.astype(float) * w[None, :, None]
        flat_y = Y.reshape(Y.shape[0], -1)[:, ok]
        pcs = (flat_y - F.mean(axis=0, keepdims=True)) @ eofs.T
        pcs = pcs / np.maximum(S[:n], 1e-9)[None, :]
        idx = pd.PeriodIndex(pd.DatetimeIndex(anom.time.values), freq="M")
        df = pd.DataFrame(pcs, index=idx, columns=[f"pc{i+1}" for i in range(n)])
        base_mask = (df.index.year >= cfg.base_start) & (df.index.year <= cfg.base_end)
        for c in df.columns:
            sd = df.loc[base_mask, c].std()
            df[c] = df[c] / (sd if np.isfinite(sd) and sd > 1e-9 else 1.0)
        self._pcs = df
        return df

    def ocean_spread(self, lead=3, sigma=0.25, n_members=40):
        key = (int(lead), float(sigma), int(n_members))
        if key not in self._ospr:
            from agrocast.features.lim import lim_ensemble_frame

            pcs = self.sst_pcs()
            idx = pcs.dropna().index
            ens = lim_ensemble_frame(pcs, idx, horizons=(int(lead),), n_members=n_members, sigma=sigma)
            cols = [f"pc{i}_sp_f{int(lead)}" for i in range(1, 4) if f"pc{i}_sp_f{int(lead)}" in ens.columns]
            if cols:
                self._ospr[key] = ens[cols].mean(axis=1).dropna()
            else:
                self._ospr[key] = pd.Series(dtype=float)
        return self._ospr[key]

    def predictor_frame(self):
        if self._pf is not None:
            return self._pf
        idx = self.indices()
        pcs = self.sst_pcs().reindex(idx.index)
        m = self.monthly()
        data = {}
        for c in IDX_COLS:
            if c not in idx.columns:
                continue
            v = idx[c].to_numpy(float)
            s3 = pd.Series(v).rolling(3, min_periods=3).mean()
            data[c + "_3m"] = s3.to_numpy()
            data[c + "_1m"] = v
            if c == "nino34":

                data["nino34_3m_s6"] = (s3 - s3.shift(6)).to_numpy()
                data["nino34_3m_e"] = (s3 * np.abs(s3)).to_numpy()
        for i, c in enumerate(pcs.columns):
            data[c] = pcs[c].to_numpy(float)
        for src, dst in [("swvl", "swvl_a"), ("snow", "snow_a"), ("t2m", "t2m_a"), ("tp", "tp_a")]:
            if src in m.columns:
                data[dst] = past_monthly_anom(m[src].reindex(idx.index)).reindex(idx.index).to_numpy(float)
        for c in ["pc1", "pc2", "pc3"]:
            if c not in pcs.columns:
                continue
            v = pcs[c].reindex(idx.index)
            for lag in (3, 6, 12, 24):
                data[f"{c}_l{lag}"] = v.shift(lag).to_numpy(float)
            data[f"{c}_s3"] = (v - v.shift(3)).to_numpy(float)
            data[f"{c}_s6"] = (v - v.shift(6)).to_numpy(float)
            data[f"{c}_s12"] = (v - v.shift(12)).to_numpy(float)
            if c == "pc1":
                data["pc1_e"] = (v * np.abs(v)).to_numpy(float)
        limf = lim_forecast_frame(pcs, idx.index)
        for c in limf.columns:
            data[c] = limf[c].to_numpy(float)
        if self.store.exists("sst"):
            sstg = _prep_lon(self.store.open("sst"))["sst"]
            for box, name in [((30, 45, 0, 40), "med_a"), ((41, 47, 27, 41), "black_a")]:
                s = _box_series(sstg, *box)
                if s is None:
                    continue
                a = past_monthly_anom(s).reindex(idx.index)
                data[name] = a.to_numpy(float)
                data[name + "_s3"] = (a - a.shift(3)).to_numpy(float)
        if self.store.exists("strat_snow"):
            sdf = self.store.open("strat_snow").to_dataframe()
            sp = pd.PeriodIndex(sdf.index, freq="M")
            for src in ("u10", "z50"):
                if src not in sdf.columns:
                    continue
                s = pd.Series(sdf[src].to_numpy("float32"), index=sp)
                a = past_monthly_anom(s).reindex(idx.index)
                data[src + "_a"] = a.to_numpy(float)
                data[src + "_a_l1"] = a.shift(1).to_numpy(float)
                data[src + "_a_l3"] = a.shift(3).to_numpy(float)
                if src == "u10":
                    data["u10_a_s3"] = (a - a.shift(3)).to_numpy(float)
        if self.store.exists("regimes"):
            rdf = self.store.open("regimes").to_dataframe()
            rp = pd.PeriodIndex(rdf.index, freq="M")
            for c in ["zpc1", "zpc2", "zpc3", "zpc4"]:
                if c not in rdf.columns:
                    continue
                s = pd.Series(rdf[c].to_numpy("float32"), index=rp).reindex(idx.index)
                data[c + "_a"] = s.to_numpy(float)
                if c in ("zpc1", "zpc2"):
                    data[c + "_a_l1"] = s.shift(1).to_numpy(float)
            if "regime" in rdf.columns:
                lab = pd.Series(rdf["regime"].to_numpy("int8"), index=rp).reindex(idx.index)
                for k in range(4):
                    oh = (lab == k).astype(float)
                    data[f"reg{k+1}"] = oh.to_numpy(float)
                    data[f"reg{k+1}_f3"] = oh.rolling(3, min_periods=3).mean().to_numpy(float)
        soil = self.soil_monthly()
        if soil is not None:
            for src, dst in [("swvl", "swvl_a"), ("snow", "snow_a")]:
                if src in soil.columns and dst not in data:
                    data[dst] = past_monthly_anom(soil[src].reindex(idx.index)).reindex(idx.index).to_numpy(float)
        lo = self.land_ocean_frame()
        if lo is not None:
            for c in lo.columns:
                data[c] = lo[c].reindex(idx.index).to_numpy(float)
        raw = pd.DataFrame(data, index=idx.index).sort_index()
        pf = past_standardize(raw)
        pf = pf.dropna()
        self._pf = pf
        return pf

    def standardized(self, variable):
        return standardize_monthly(self.monthly()[variable], self.config.clim_window, self.config.clim_half_life)

    def seasonal_raw(self, variable, n_months=3):
        key = (variable, int(n_months))
        if key not in self._seas_raw:
            self._seas_raw[key] = seasonal_series(self.monthly(), variable, key[1])
        return self._seas_raw[key]

    def seasonal_std(self, variable, n_months=3):
        key = (variable, int(n_months))
        if key not in self._seas_std:
            self._seas_std[key] = standardize_monthly(
                self.seasonal_raw(variable, key[1]), self.config.clim_window, self.config.clim_half_life
            )
        return self._seas_std[key]


_REGIME_CORE = {"zpc1_a", "zpc2_a"}


def feature_columns_for(pf, variable, mode="seasonal"):
    cols = list(pf.columns)
    if variable == "tp":
        pats = [
            re.compile(r"^zpc[34]_a$"),
            re.compile(r"^zpc[12]_a_l[0-9]+$"),
            re.compile(r"^reg[0-9](_f3)?$"),
        ]
        return [c for c in cols if not any(p.match(c) for p in pats)]
    elif mode == "monthly":
        pats = [re.compile(r"^pc[123]_f[0-9]+$"), re.compile(r"^zpc[0-9]_a(_l[0-9]+)?$")]
        cols = [c for c in cols if not any(p.match(c) for p in pats)]
    cols = [c for c in cols if not re.match(r"^reg[0-9](_f3)?$", c)]
    return cols


def make_test_row(pf, issue, lead, target_period, use_cols=None):
    base = pf.loc[issue]
    cols = list(pf.columns) if use_cols is None else list(use_cols)
    tmonth = (target_period - (lead - 1)).month
    vals = list(np.asarray(base[cols].to_numpy(), float)) + [lead / 12.0, float(tmonth)]
    return pd.Series(vals, index=cols + ["lead", "tmonth"])


def training_data(pf, std_df, variable, start_month, lead, until, use_cols=None):
    cols = (list(pf.columns) if use_cols is None else list(use_cols)) + ["lead"]
    X, y, years, periods, e1s, e2s, mus, sds = [], [], [], [], [], [], [], []
    s = std_df["z"]
    first_year = pf.index[0].year
    last_year = until.year + 1
    for y0 in range(first_year, last_year + 1):
        try:
            start = pd.Period(f"{y0}-{int(start_month):02d}", "M")
        except ValueError:
            continue
        tgt = start + (lead - 1)
        issue = start - 1
        if issue not in pf.index or tgt not in s.index:
            continue
        row = pf.loc[issue]
        if use_cols is not None:
            row = row[use_cols]
        if not np.isfinite(row.to_numpy(float)).all():
            continue
        if tgt > until:
            continue
        z = float(s.loc[tgt])
        if not np.isfinite(z):
            continue
        meta = std_df.loc[tgt]
        X.append(list(row.to_numpy(float)) + [lead / 12.0, float(start_month)])
        y.append(z)
        years.append(tgt.year)
        periods.append(tgt)
        e1s.append(float(meta["e1"]))
        e2s.append(float(meta["e2"]))
        mus.append(float(meta["mu"]))
        sds.append(float(meta["sd"]))
    Xdf = pd.DataFrame(X, columns=cols + ["tmonth"])
    meta_df = pd.DataFrame(
        {"year": years, "period": periods, "e1": e1s, "e2": e2s, "mu": mus, "sd": sds}
    )
    return Xdf, np.asarray(y, float), meta_df
