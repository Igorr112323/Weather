import numpy as np
import pandas as pd

from agrocast.blend.conformal import ConformalQuantileCalibrator


def _records(n=800, seed=0):
    rng = np.random.default_rng(seed)
    obs = rng.normal(0, 1, n)
    # Модель систематически завышает q50 на 0.6 и сужает коридор
    q50 = obs - 0.6 + rng.normal(0, 0.3, n)
    q10 = q50 - 0.5
    q90 = q50 + 0.5
    return pd.DataFrame(
        {
            "variable": "t2m",
            "lead": rng.integers(1, 7, n),
            "q10": q10,
            "q50": q50,
            "q90": q90,
            "obs_z": obs,
        }
    )


def _cov(q, o):
    return float(np.mean((q[:, 0] - 1e-9 <= o) & (o <= q[:, 2] + 1e-9)))


def test_conformal_fixes_coverage():
    rec = _records()
    q_raw = rec[["q10", "q50", "q90"]].to_numpy()
    cov_before = _cov(q_raw, rec["obs_z"].to_numpy())
    ccal = ConformalQuantileCalibrator().fit(rec)
    assert ccal.usable()
    q_new = np.array([ccal.transform([a, b, c], "t2m", l) for a, b, c, l in zip(rec.q10, rec.q50, rec.q90, rec.lead)])
    cov_after = _cov(q_new, rec["obs_z"].to_numpy())
    assert cov_after > cov_before + 0.05, (cov_before, cov_after)
    assert abs(cov_after - 0.80) < 0.08, cov_after
    assert abs(np.median(rec["obs_z"] - q_new[:, 1])) < 0.25


def test_conformal_small_n_fallback():
    rec = _records(30)
    ccal = ConformalQuantileCalibrator().fit(rec)
    # мало данных по горизонтам, но ALL есть (n=30 < MIN_N=60 -> None)
    q = ccal.transform([0.0, 0.0, 0.0], "t2m", 2)
    assert np.allclose(q, [0.0, 0.0, 0.0])


def test_conformal_save_load(tmp_path):
    rec = _records()
    ccal = ConformalQuantileCalibrator().fit(rec)
    p = tmp_path / "c.json"
    ccal.save(p)
    c2 = ConformalQuantileCalibrator.load(p)
    q1 = ccal.transform([0.1, 0.3, 0.6], "t2m", 4)
    q2 = c2.transform([0.1, 0.3, 0.6], "t2m", 4)
    assert np.allclose(q1, q2)
