import numpy as np
import pandas as pd

from agrocast.core.mathutils import softmax_w
from agrocast.backtest.metrics import weighted_rpss, year_weights

MIN_N = 30
SHRINK = 0.28


def attach_obs(blend, rec, keys=("variable", "lead", "year", "target_month")):
    """Подтянуть obs_z из исходных записей: наблюдение одно для всех моделей
    в группе (variable, lead, year, target_month)."""
    obs = rec.drop_duplicates(list(keys))[list(keys) + ["obs_z"]]
    return blend.merge(obs, on=list(keys), how="left")
SEASONS = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM", 6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def season_of(month):
    return SEASONS.get(int(month), "ALL")


def blended_records(records, weights):
    rows = []
    for key, g in records.groupby(["variable", "target_month", "lead", "year"]):
        v = key[0]
        month = int(key[1])
        vw = weights.get(v, {})
        wmap = vw.get(season_of(month), vw.get("ALL", {}))
        probs = np.zeros(3)
        qs = np.zeros(3)
        tot = 0.0
        clim_q = None
        for name, gg in g.groupby("model"):
            qv = gg[["q10", "q50", "q90"]].to_numpy()[0]
            if name == "clim":
                clim_q = qv
            wv = float(wmap.get(name, 0.0))
            probs += wv * gg[["p0", "p1", "p2"]].to_numpy()[0]
            qs += wv * qv
            tot += wv
        if tot <= 0:
            probs = np.full(3, 1.0 / 3.0)
            qs = clim_q if clim_q is not None else np.zeros(3)
        else:
            probs /= tot
            probs = (1.0 - SHRINK) * probs + SHRINK / 3.0
            qs /= tot
        rows.append((v, month, key[2], key[3], probs[0], probs[1], probs[2], qs[0], qs[1], qs[2], int(g["obs_tercile"].iloc[0])))
    return pd.DataFrame(rows, columns=["variable", "target_month", "lead", "year", "p0", "p1", "p2", "q10", "q50", "q90", "obs_tercile"])


def _level_weights(g, k=15):
    r = g["rpss"].to_numpy(float)
    names = g["model"].tolist()
    ns = g["n"].to_numpy(int)
    r_zeroed = np.where(ns < MIN_N, 0.0, r)
    r_adj = r_zeroed * ns / (ns + k)
    positive = r_adj > 0.005
    if positive.any():
        rr = np.where(positive, r_adj, 0.0)
        sw = softmax_w(rr, 0.05)
        sw = np.where(positive, sw, 0.0)
        sw = sw / sw.sum()
        return {n: float(wv) for n, wv in zip(names, sw)}
    # Честный пол (skill floor): ни одна модель не показала навык —
    # нулевые веса для всех => бленд деградирует до чистой климатологии,
    # а значит система структурно не может быть хуже климатологии.
    return {n: 0.0 for n in names}


class Blender:
    def __init__(self, half_life_years=0.0):
        self.weights = {}
        self.skill = None
        self.half_life_years = float(half_life_years)

    def fit(self, records):
        rec = records.copy()
        if rec.empty:
            return self
        rec["season"] = rec["target_month"].map(season_of)
        rows = []
        for keys, g in rec.groupby(["variable", "season", "model"]):
            obs = g["obs_tercile"].to_numpy(int)
            probs = g[["p0", "p1", "p2"]].to_numpy(float)
            w = year_weights(g["year"], self.half_life_years)
            rpss = weighted_rpss(probs, obs, w, months=(g["target_month"].to_numpy(int) if "target_month" in g.columns else None))
            rows.append({"variable": keys[0], "season": keys[1], "model": keys[2], "rpss": float(rpss), "n": len(g)})
        skill = pd.DataFrame(rows)
        self.skill = skill
        weights = {}
        for v, gv in skill.groupby("variable"):
            var_rows = gv.groupby("model", as_index=False).agg(rpss=("rpss", "mean"), n=("n", "sum"))
            all_w = _level_weights(var_rows)
            weights[v] = {"ALL": all_w}
            for season, gs in gv.groupby("season"):
                tot_n = int(gs["n"].sum())
                if tot_n < MIN_N:
                    weights[v][season] = all_w
                else:
                    local = _level_weights(gs)
                    alpha = tot_n / (tot_n + 40.0)
                    weights[v][season] = {
                        m: alpha * local.get(m, 0.0) + (1.0 - alpha) * all_w.get(m, 0.0)
                        for m in set(local) | set(all_w)
                    }
        self.weights = weights
        return self

    @classmethod
    def default(cls, variables=("t2m", "tp")):
        b = cls()
        for v in variables:
            b.weights[v] = {"ALL": {"clim": 0.4, "analog": 0.2, "ridge": 0.2, "gbm": 0.2}}
        return b

    def combine(self, variable, month, preds):
        vw = self.weights.get(variable, {})
        wmap = vw.get(season_of(month), vw.get("ALL", {}))
        P = np.zeros(3)
        Q = np.zeros(3)
        tot = 0.0
        for name, (p, q) in preds.items():
            w = float(wmap.get(name, 0.0))
            P += w * np.asarray(p, float)
            Q += w * np.asarray(q, float)
            tot += w
        if tot <= 0:
            q = preds.get("clim", (None, np.zeros(3)))[1]
            return np.full(3, 1.0 / 3.0), np.asarray(q, float)
        P = P / tot
        P = (1.0 - SHRINK) * P + SHRINK / 3.0
        return P, Q / tot

    def save(self, path):
        from agrocast.core.artifacts import BLENDER_SCHEMA, write_artifact
        from agrocast.models.builder import MODEL_NAMES

        write_artifact(path, {"weights": self.weights, "half_life_years": self.half_life_years}, BLENDER_SCHEMA, models=MODEL_NAMES)

    @classmethod
    def load(cls, path):
        from agrocast.core.artifacts import BLENDER_SCHEMA, check_weight_models, read_artifact
        from agrocast.models.builder import MODEL_NAMES

        data = read_artifact(path, schema=BLENDER_SCHEMA, name="blender", known_models=MODEL_NAMES)
        if data is None:
            return None
        b = cls()
        b.weights = data["weights"]
        check_weight_models(b.weights, MODEL_NAMES, name="blender")
        return b

