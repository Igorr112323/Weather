import json

import numpy as np

GRID = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
SEL0, SEL1 = 2004, 2014


def stack_path(config, mode, variable):
    return config.artifact_dir / f"stack_{mode}_{variable}.json"


def load_alpha(config, mode, variable):
    p = stack_path(config, mode, variable)
    if not p.exists():
        return 0.0
    try:
        return float(json.loads(p.read_text()).get("alpha", 0.0))
    except Exception:
        return 0.0


def save_alpha(config, mode, variable, alpha):
    stack_path(config, mode, variable).write_text('{"alpha": %s}' % round(float(alpha), 3))


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
    years_arr = np.array(sorted(set(yrows.tolist())))
    clim = np.tile([1 / 3.0] * 3, (len(obs), 1))
    best_a, best_key = None, None
    for a in grid:
        Pm = mix(Prows, keys, nnmap, a)
        cal = TercileCalibrator().fit(Pm, obs)
        Pc = cal.transform(Pm) if cal.usable() else Pm
        rps_p = rps_rows(Pc, obs)
        rps_c = rps_rows(clim, obs)
        year_rpss = np.array([1.0 - float(rps_p[yrows == y].mean()) / float(rps_c[yrows == y].mean()) for y in years_arr])
        sel = year_rpss[(years_arr >= SEL0) & (years_arr <= SEL1)]
        key = (int((sel <= 0).sum()), int((year_rpss <= 0).sum()), float(-sel.min()), float(-sel.mean()))
        if best_key is None or key < best_key:
            best_key, best_a = key, float(a)
    return best_a
