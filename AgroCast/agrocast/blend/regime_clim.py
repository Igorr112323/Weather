import json

import numpy as np
import pandas as pd

from agrocast.blend.blender import season_of

SPECS = {
    "seasonal": {
        "t2m": {
            "feats": ["nino34_3m", "zt3", "zt6", "zp3", "zp6", "trend", "nao_3m", "iod_3m"],
            "C": 0.02,
            "w0": 0.2,
            "w1": 1.2,
            "sdiv": 1.2,
            "min_n": 15,
            "mems": (3, 6),
            "xmems": (("tp", 3), ("tp", 6)),
        },
        "tp": {
            "feats": ["nino34_3m", "zp3", "zp6", "h1", "iod_3m", "amo_3m"],
            "C": 3.0,
            "w": 0.05,
            "sdiv": 1.2,
            "min_n": 18,
            "mems": (3, 6),
            "xmems": (),
        },
    },
    "monthly": {
    },
}

REF0, REF1 = 1991, 2010


def memory_z(monthly, v, k, ref0=REF0, ref1=REF1):
    s = monthly[v].astype(float)
    ref = s[(s.index.year >= ref0) & (s.index.year <= ref1)]
    mu, sd = float(ref.mean()), float(ref.std())
    m = s.rolling(k, min_periods=k).mean()
    return (m - mu) / sd


def season_issue_key(target_month, year):
    if target_month == 1:
        return (year - 1) * 100 + 11
    if target_month == 4:
        return year * 100 + 2
    if target_month == 7:
        return year * 100 + 5
    return year * 100 + 8


def issue_key_for(target_month, year, lead):
    tm = target_month - lead
    if tm <= 0:
        return (year - 1) * 100 + (12 + tm)
    return year * 100 + tm


def tercile_grid(point, v, window=3):
    d = {}
    for dt, r in point.seasonal_std(v, window).iterrows():
        d.setdefault(int(dt.year), {})[int(dt.month)] = int(
            0 if r["z"] < r["e1"] else (1 if r["z"] <= r["e2"] else 2)
        )
    return d


def h1_tercile(terc, issue):
    if not terc or issue is None or issue.month <= 1:
        return 0.5
    row = terc.get(int(issue.year), {})
    vals = [row[m] for m in range(1, issue.month) if m in row]
    if not vals:
        return 0.5
    return float(np.mean(vals))


def prev_issue_key(key):
    y, m = key // 100, key % 100
    y2, m2 = y, m - 3
    if m2 <= 0:
        y2, m2 = y2 - 1, m2 + 12
    return y2 * 100 + m2


def _issue_key(mode, target_month, year, lead):
    if mode == "seasonal":
        return season_issue_key(target_month, year)
    return issue_key_for(target_month, year, lead)


class RegimeClimatology:
    def __init__(self, mode="seasonal", groups=None, history=None):
        self.mode = mode
        self.groups = groups or {}
        self.history = history or {}

    @classmethod
    def fit_history(cls, mode, point, leads=(1, 2, 3, 4, 5, 6), until_period=None):
        out = cls(mode)
        spec_map = SPECS.get(mode, {})
        if not spec_map:
            return out
        mon = point.monthly()
        pf = point.predictor_frame().reset_index()
        pf["key"] = pf["index"].dt.year * 100 + pf["index"].dt.month
        mems = {}
        terc = {}
        for v, spec in spec_map.items():
            for k in spec["mems"]:
                mems[(v, k)] = memory_z(mon, v, k).to_dict()
            for vx, k in spec.get("xmems", ()):
                if (vx, k) not in mems:
                    mems[(vx, k)] = memory_z(mon, vx, k).to_dict()
            terc[v] = tercile_grid(point, v)
        for v, spec in spec_map.items():
            std = point.seasonal_std(v, 3)
            rows = []
            for dt, r in std.iterrows():
                if until_period is not None and dt > until_period:
                    break
                tm = int(dt.month)
                y = int(dt.year)
                if mode == "seasonal":
                    if tm not in (1, 4, 7, 10):
                        continue
                    grp = season_of(tm)
                    lead_set = (1,)
                else:
                    grp = f"m{tm}"
                    lead_set = tuple(leads)
                for lead in lead_set:
                    key = _issue_key(mode, tm, y, lead)
                    match = pf[pf["key"] == key]
                    if len(match) == 0:
                        continue
                    issue_period = match["index"].iloc[0]
                    irow = match.iloc[0]
                    feats = {}
                    for k in spec["mems"]:
                        feats["z" + ("t" if v == "t2m" else "p") + str(k)] = float(
                            mems[(v, k)].get(issue_period, np.nan)
                        )
                    for vx, k in spec.get("xmems", ()):
                        feats["z" + ("t" if vx == "t2m" else "p") + str(k)] = float(
                            mems[(vx, k)].get(issue_period, np.nan)
                        )
                    if "trend" in spec["feats"]:
                        m3 = pf[pf["key"] == prev_issue_key(key)]
                        if len(m3) == 0:
                            feats["trend"] = np.nan
                        else:
                            feats["trend"] = float(irow["nino34_3m"] - m3.iloc[0]["nino34_3m"])
                    if "h1" in spec["feats"]:
                        feats["h1"] = h1_tercile(terc[v], issue_period)
                    for f in spec["feats"]:
                        if f in feats:
                            continue
                        if f in irow.index:
                            feats[f] = float(irow[f])
                        else:
                            feats[f] = np.nan
                    if any(np.isnan(x) for x in feats.values()):
                        continue
                    rows.append(
                        {
                            "year": y,
                            "grp": grp,
                            "lead": int(lead),
                            "obs": int(0 if r["z"] < r["e1"] else (1 if r["z"] <= r["e2"] else 2)),
                            **feats,
                        }
                    )
            if rows:
                df = pd.DataFrame(rows)
                for g in df["grp"].unique():
                    out.history[(v, g)] = df[df["grp"] == g].reset_index(drop=True)
        return out

    def prior_for(self, variable, group, year, x):
        from sklearn.linear_model import LogisticRegression

        spec = SPECS.get(self.mode, {}).get(variable)
        if spec is None:
            return None
        h = self.history.get((variable, group))
        if h is None or len(h) == 0:
            return None
        tr = h[h["year"] < year]
        if len(tr) < spec["min_n"]:
            return None
        X = tr[spec["feats"]].to_numpy(float)
        yv = tr["obs"].to_numpy(int)
        model = LogisticRegression(C=spec["C"], max_iter=2000)
        try:
            model.fit(X, yv)
        except Exception:
            return self._fallback_dist(h, year)
        return model.predict_proba(np.asarray(x, float).reshape(1, -1))[0]

    def _fallback_dist(self, h, year):
        tr = h[h["year"] < year]
        if len(tr) == 0:
            return None
        recent = tr[tr["year"] >= tr["year"].max() - 10]
        if len(recent) < 8:
            recent = tr
        return np.array([(recent["obs"] == t).mean() for t in range(3)])

    def _weight(self, spec, variable, x):
        if "w0" in spec:
            feats = spec["feats"]
            nino = abs(float(x[0]))
            mem6 = "zt6" if variable == "t2m" else "zp6"
            mem = abs(float(x[feats.index(mem6)])) if mem6 in feats else 0.0
            s = min(1.0, max(nino, mem) / spec["sdiv"])
            return min(0.9, spec["w0"] + spec["w1"] * s)
        return spec["w"]

    def coefficients_for(self, variable, group, until=None):
        from sklearn.linear_model import LogisticRegression

        spec = SPECS.get(self.mode, {}).get(variable)
        if spec is None:
            return None
        h = self.history.get((variable, group))
        if h is None or len(h) == 0:
            return None
        tr = h if until is None else h[h["year"] < until]
        if len(tr) < spec["min_n"]:
            return None
        X = tr[spec["feats"]].to_numpy(float)
        yv = tr["obs"].to_numpy(int)
        model = LogisticRegression(C=spec["C"], max_iter=2000)
        try:
            model.fit(X, yv)
        except Exception:
            return None
        recent = tr[tr["year"] >= tr["year"].max() - 10]
        if len(recent) < 8:
            recent = tr
        out = {
            "coefs": model.coef_.tolist(),
            "intercepts": model.intercept_.tolist(),
            "feats": spec["feats"],
            "min_n": spec["min_n"],
            "mean_dist": [float((recent["obs"] == t).mean()) for t in range(3)],
            "n": int(len(tr)),
        }
        if "w" in spec:
            out["w"] = spec["w"]
        if "w0" in spec:
            out["w0"] = spec["w0"]
            out["w1"] = spec["w1"]
            out["sdiv"] = spec["sdiv"]
        return out

    def transform(self, P, variable, group, year, x):
        spec = SPECS.get(self.mode, {}).get(variable)
        if spec is None:
            return P
        if x is None or any(np.isnan(vv) for vv in x):
            return P
        g = self.groups.get((variable, group))
        if g is not None:
            p = _softmax(np.asarray(g["coefs"], float) @ np.asarray(x, float) + np.asarray(g["intercepts"], float))
            w = self._weight(g, variable, x) if "w0" in g else g["w"]
        else:
            p = self.prior_for(variable, group, year, x)
            if p is None:
                return P
            w = self._weight(spec, variable, x)
        out = (1.0 - w) * np.asarray(P, float) + w * p
        return out / out.sum()

    def feature_vector(self, variable, pf_row, issue, mz, pf_prev_row=None, terc=None):
        spec = SPECS.get(self.mode, {}).get(variable)
        if spec is None:
            return None
        vals = []
        for f in spec["feats"]:
            if f == "trend":
                if pf_prev_row is None:
                    vals.append(np.nan)
                else:
                    vals.append(float(pf_row["nino34_3m"] - pf_prev_row["nino34_3m"]))
            elif f == "h1":
                vals.append(h1_tercile(terc, issue))
            else:
                s = mz.get(f)
                if s is not None and issue in s.index:
                    vals.append(float(s.loc[issue]))
                elif pf_row is not None and f in pf_row.index:
                    vals.append(float(pf_row[f]))
                else:
                    vals.append(np.nan)
        return vals

    def save_live(self, path):
        out = {"mode": self.mode, "groups": {}}
        for (v, g), h in sorted(self.history.items()):
            c = self.coefficients_for(v, g)
            if c is not None:
                out["groups"][f"{v}|{g}"] = c
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1))

    @classmethod
    def load(cls, path):
        if not path.exists():
            return cls("seasonal")
        d = json.loads(path.read_text())
        out = cls(d.get("mode", "seasonal"))
        for key, c in d.get("groups", {}).items():
            v, g = key.split("|")
            out.groups[(v, g)] = c
        return out


def _softmax(z):
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()
