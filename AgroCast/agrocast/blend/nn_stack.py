
import numpy as np

GRID = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
SEL0, SEL1 = 2004, 2014
VAL0, VAL1 = 2015, 2024


def stack_path(config, mode, variable):
    return config.artifact_dir / f"stack_{mode}_{variable}.json"


def load_alpha(config, mode, variable):
    from agrocast.core.artifacts import STACK_SCHEMA, read_artifact
    from agrocast.core.policy import nn_alpha_allowed

    if not nn_alpha_allowed(variable, config):
        return 0.0
    p = config.artifact_path(f"stack_{mode}_{variable}.json")
    data = read_artifact(p, schema=STACK_SCHEMA, name="nn stack")
    if data is None:
        return 0.0
    return float(data.get("alpha", 0.0))


def save_alpha(config, mode, variable, alpha):
    from agrocast.core.artifacts import STACK_SCHEMA, write_artifact

    write_artifact(stack_path(config, mode, variable), {"alpha": round(float(alpha), 3)}, STACK_SCHEMA)


def nn_map(pt, v, mode, years):
    from agrocast.features.dataset import feature_columns_for
    from agrocast.models.nn_kernel import PooledNN

    pf = pt.predictor_frame()
    std = pt.seasonal_std(v, 3) if mode == "seasonal" else pt.standardized(v)
    pool = feature_columns_for(pf, v, mode=mode)
    out = {}
    for Y in years:
        nn = PooledNN().fit(pf, std, pool, mode, int(Y))
        if not nn.usable():
            continue
        for tgt in std.index:
            if int(tgt.year) != int(Y):
                continue
            for lead in ([1] if mode == "seasonal" else range(1, 7)):
                iss = tgt - lead
                if iss not in pf.index:
                    continue
                Pn = nn.probs_for(pf, iss, pool, tgt.month, lead)
                if Pn is not None:
                    out[(int(tgt.year), int(tgt.month), int(lead))] = np.asarray(Pn, float)
    return out


def row_keys(df):
    return [(int(r["year"]), int(r["target_month"]), int(r["lead"])) for _, r in df.iterrows()]


def mix(P, keys, nnmap, alpha):
    P = np.array(P, float)
    if alpha <= 0:
        return P
    for i, k in enumerate(keys):
        if k in nnmap:
            P[i] = (1.0 - alpha) * P[i] + alpha * nnmap[k]
    return P


def select_alpha(pt, v, mode, blend, grid=GRID, nnmap=None):
    if v == "tp":
        return 0.0
    from agrocast.backtest.metrics import rps_rows
    from agrocast.blend.calibration import TercileCalibrator

    g = blend[blend.variable == v]
    if g.empty:
        return 0.0
    years = sorted(g["year"].unique())
    if nnmap is None:
        nnmap = nn_map(pt, v, mode, years)
    keys = row_keys(g)
    Prows = g[["p0", "p1", "p2"]].to_numpy(float)
    obs = g["obs_tercile"].to_numpy(int)
    yrows = g["year"].to_numpy(int)
    sel_mask = (yrows >= SEL0) & (yrows <= SEL1)
    val_mask = (yrows >= VAL0) & (yrows <= VAL1)
    if not sel_mask.any() or not val_mask.any():
        return 0.0
    sel_rps = {}
    val_rps = {}
    for a in grid:
        Pm = mix(Prows, keys, nnmap, a)
        cal = TercileCalibrator().fit(Pm[sel_mask], obs[sel_mask])
        if cal.usable():
            sel_rps[a] = float(rps_rows(cal.transform(Pm[sel_mask]), obs[sel_mask]).mean())
            val_rps[a] = float(rps_rows(cal.transform(Pm[val_mask]), obs[val_mask]).mean())
        else:
            sel_rps[a] = float(rps_rows(Pm[sel_mask], obs[sel_mask]).mean())
            val_rps[a] = float(rps_rows(Pm[val_mask], obs[val_mask]).mean())
    candidates = [0.0] + [a for a in grid if a != 0.0 and val_rps[a] < val_rps[0.0]]
    return float(min(candidates, key=lambda a: sel_rps[a]))
