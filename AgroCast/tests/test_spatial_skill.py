import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agrocast.backtest.engine import EVAL_REPLAY_REVISED  # noqa: E402
from agrocast.skill import spatial  # noqa: E402

WORLD = ROOT / "world"

CELLS = [
    ("A", 44.25, 37.25),
    ("B", 44.25, 37.75),
    ("C", 44.75, 37.25),
    ("D", 44.75, 37.75),
    ("E", 45.25, 37.25),
    ("F", 45.25, 37.75),
]


def _toy_pipe(a_obs=(1, 1, 1, 1), a_obs_z=1.0, a_sd=2.0, a_p=(0.2, 0.5, 0.3)):
    rows = []
    periods = ["2018-03", "2019-03", "2020-03", "2021-03"]
    for cid, lat, lon in CELLS:
        mu, sd = (1.0, a_sd) if cid == "A" else (0.0, 1.0)
        pp = a_p if cid == "A" else (0.5, 0.3, 0.2)
        for i, tp in enumerate(periods):
            obs_terc = a_obs[i] if cid == "A" else (0, 1, 2, 0)[i]
            oz = a_obs_z if cid == "A" else 0.5
            for v in ("t2m", "tp"):
                for lead in (1, 2):
                    rows.append(
                        {
                            "variable": v, "lead": lead, "mode": "monthly",
                            "start_month": 3, "target_month": 3, "year": int(tp[:4]),
                            "issue": tp, "target_start": tp, "target_end": tp,
                            "p0": pp[0], "p1": pp[1], "p2": pp[2],
                            "q10": -1.28, "q50": 0.0, "q90": 1.28,
                            "obs_z": oz, "obs_tercile": obs_terc, "mu": mu, "sd": sd,
                            "cell_lat": lat, "cell_lon": lon, "cell_id": cid,
                        }
                    )
    return pd.DataFrame(rows)


def _cell_metrics(pipe, cid):
    g = pipe[(pipe["cell_id"] == cid) & (pipe["variable"] == "t2m") & (pipe["lead"] == 1)]
    P = g[["p0", "p1", "p2"]].to_numpy(float)
    obs = g["obs_tercile"].to_numpy(int)
    lo = (g["mu"] + g["sd"] * g["q10"]).to_numpy(float)
    hi = (g["mu"] + g["sd"] * g["q90"]).to_numpy(float)
    o = (g["mu"] + g["sd"] * g["obs_z"]).to_numpy(float)
    return {
        "obs": obs.tolist(),
        "rps": spatial.rps_rows(P, obs).tolist(),
        "hit": float((P.argmax(axis=1) == obs).mean()),
        "coverage": float(((o >= lo) & (o <= hi)).mean()),
    }


def test_local_fact_changes_only_that_cell():
    base = _toy_pipe()
    pipe = _toy_pipe(a_obs=(1, 1, 2, 1), a_obs_z=4.0)
    assert _cell_metrics(base, "A")["coverage"] == 1.0

    a0, b0 = _cell_metrics(base, "A"), _cell_metrics(base, "B")
    a1, b1 = _cell_metrics(pipe, "A"), _cell_metrics(pipe, "B")

    assert a0["obs"] != a1["obs"]
    assert a0["rps"] != a1["rps"]
    assert a0["hit"] == 1.0 and a1["hit"] == 0.75
    assert a0["coverage"] == 1.0 and a1["coverage"] == 0.0
    assert b0 == b1

    pp0 = {r["id"]: r for r in spatial.per_point_summary(base)}
    pp1 = {r["id"]: r for r in spatial.per_point_summary(pipe)}
    assert pp0["A"]["monthly_t2m_l1_hit"] != pp1["A"]["monthly_t2m_l1_hit"]
    assert pp0["A"]["monthly_t2m_l1_rps"] != pp1["A"]["monthly_t2m_l1_rps"]
    for cid in ("B", "C", "D", "E", "F"):
        assert pp0[cid] == pp1[cid]


def test_local_baseline_is_per_cell():
    pipe = _toy_pipe()
    ev_a = spatial.evaluate_combo(pipe, "t2m", 1)
    is_a = (ev_a["frame"]["cell_id"] == "A").to_numpy()
    base_a = float(ev_a["rps_base"][is_a].mean())
    base_other = float(ev_a["rps_base"][~is_a].mean())
    assert abs(base_a - base_other) > 1e-9, "бейзлайн ячейки A (константный класс) обязан отличаться от чередующихся ячеек"


def test_all_combos_published_with_blocks():
    combos = spatial.summarize(_toy_pipe(), modes=["monthly"], leads={"monthly": [1, 2]}, variables=("t2m", "tp"), n_boot=160, seed=7)
    assert set(combos) == {"monthly_t2m_l1", "monthly_t2m_l2", "monthly_tp_l1", "monthly_tp_l2"}
    for key, c in combos.items():
        assert c["n_rows"] == 24 and c["n_cells"] == 6 and c["n_periods"] == 4
        assert c["n_time_blocks"] == 2 and c["block_len_months"] == 12
        assert c["n_spatial_blocks"] == 2
        assert spatial.METHOD in c["method"]
        assert "climatology" in c["baseline"] and "local empirical" in c["baseline"]
        assert c["evaluation"] == EVAL_REPLAY_REVISED
        lo, hi = c["rps_ci95_time"]
        assert lo is not None and lo <= hi
        assert c["rpss"] is not None
        if c["variable"] == "tp":
            assert c["hit"] == pytest.approx((1.0 + 0.5 * 5) / 6)
            assert c["coverage80"] == pytest.approx(1.0)
            assert c["rps"] == pytest.approx((0.13 * 4 + 1.76 * 5) / 24, abs=1e-9)


def test_block_len_defaults_and_overlap():
    monthly = spatial.summarize(_toy_pipe(), modes=["monthly"], leads={"monthly": [1]}, n_boot=60, seed=3)
    assert monthly["monthly_t2m_l1"]["block_len_months"] == 12
    seasonal_pipe = _toy_pipe()
    seasonal_pipe["mode"] = "seasonal"
    seasonal = spatial.summarize(seasonal_pipe, modes=["seasonal"], leads={"seasonal": [1]}, n_boot=60, seed=3)
    c = seasonal["seasonal_t2m_l1"]
    assert c["block_len_months"] == 6
    assert 1 <= c["n_time_blocks"] < c["n_rows"]
    assert c["n_spatial_blocks"] < c["n_cells"]


def test_artifact_legacy_keys_and_roundtrip(tmp_path):
    pipe = _toy_pipe()
    combos = spatial.summarize(pipe, modes=["monthly"], leads={"monthly": [1, 2]}, n_boot=80, seed=7)
    by_point = spatial.per_point_summary(pipe)
    art = spatial.build_skill_artifact(combos, by_point, "krai", "Краснодарский край", "2018-2021", 6, "тест")
    assert art["schema"] == "grid-skill-v2"
    assert art["verifications"] == sum(c["n_rows"] for c in combos.values()) == 96
    for name in ("monthly_t2m", "monthly_tp"):
        assert art[name] is not None and art[name]["n"] == 24
    assert "seasonal_t2m" not in art
    assert set(art["combos"]) == set(combos)
    assert art["spatial"]["n_spatial_blocks"] == 2
    assert sorted(art["spatial"]["n_time_blocks"].values()) == [2, 2, 2, 2]
    assert art["spatial"]["evaluation"] == [EVAL_REPLAY_REVISED]
    out = tmp_path / "krai_grid_skill.json"
    spatial.write_artifact(out, art)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == json.loads(json.dumps(art))
    assert loaded["by_point"][0]["id"] in {"A", "B", "C", "D", "E", "F"}


def test_legacy_block_content_matches_combos():
    pipe = _toy_pipe()
    combos = spatial.summarize(pipe, modes=["monthly"], leads={"monthly": [1]}, n_boot=80, seed=7)
    art = spatial.build_skill_artifact(combos, [], "krai", "KR", "2018-2021", 6, "тест")
    blk = art["monthly_t2m"]
    c = combos["monthly_t2m_l1"]
    assert blk["rpss"] == c["rpss"] and blk["hit"] == c["hit"]
    assert blk["conformal_coverage"] == c["coverage80"]
    assert blk["hit_ci95"] == c["hit_ci95_time"]
    assert blk["years"] == "2018-2021" and blk["n"] == c["n_rows"]


def test_bootstrap_deterministic_with_seed():
    pipe = _toy_pipe()
    kw = dict(modes=["monthly"], leads={"monthly": [1]}, n_boot=120)
    c1 = spatial.summarize(pipe, seed=11, **kw)
    c2 = spatial.summarize(pipe, seed=11, **kw)
    c3 = spatial.summarize(pipe, seed=12, **kw)
    assert c1["monthly_t2m_l1"]["rps_ci95_time"] == c2["monthly_t2m_l1"]["rps_ci95_time"]
    assert c1["monthly_t2m_l1"]["rps_ci95_time"] != c3["monthly_t2m_l1"]["rps_ci95_time"]


def test_engine_pipeline_table_has_local_facts(tmp_path):
    from agrocast.backtest.engine import run_backtest
    from agrocast.serve.pipeline import world_config

    cfg = world_config(WORLD, tmp_path)
    store = cfg.zarr_store()
    from agrocast.region import regions

    cells = regions.region_cells(store, region="krai")[:1]
    assert cells
    pid, lat, lon = cells[0]["id"], float(cells[0]["lat"]), float(cells[0]["lon"])
    _, pipe = run_backtest(cfg, variables=("t2m",), start_months=[12], leads=[1], years=[2025], mode="seasonal",
                           lat=float(lat), lon=float(lon), save_artifacts=False, return_pipeline=True)
    assert not pipe.empty
    for col in ("mu", "sd", "obs_z", "obs_tercile", "evaluation", "q10", "q90"):
        assert col in pipe.columns
    g = spatial.add_cell_frame(pipe.assign(mode="seasonal"), lat, lon, pid)
    combos = spatial.summarize(g, modes=["seasonal"], leads={"seasonal": [1]}, variables=("t2m",), n_boot=60, seed=5)
    c = combos["seasonal_t2m_l1"]
    assert c["n_cells"] == 1 and c["n_spatial_blocks"] == 1
    assert c["n_time_blocks"] == max(1, int(np.ceil(c["n_rows"] / 6)))
    assert c["evaluation"] == EVAL_REPLAY_REVISED or c["evaluation"] == "prospective"
