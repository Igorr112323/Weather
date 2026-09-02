import numpy as np
import pandas as pd

from agrocast.models.ssw import SSWModel, SSW_Z, ssw_month_flags


def test_ssw_flags_on_real_series():
    import xarray as xr
    from pathlib import Path

    world = Path(__file__).resolve().parents[1] / "world" / "zarr" / "strat_snow"
    ss = xr.open_zarr(str(world))
    u = ss.u10.values
    z = (u - u.mean()) / u.std()
    flags = ssw_month_flags(pd.Series(z)).to_numpy()
    n = int(flags.sum())
    # 1948–2026: ослабленный вихрь ~15–40% месяцев — реалистичный порядок
    assert 0.10 < n / len(z) < 0.45, n / len(z)


def test_ssw_model_fit_predict():
    rng = np.random.default_rng(1)
    n = 120
    u10 = rng.normal(0, 1, n)
    u10_l = np.roll(u10, 1)
    # «правда»: после ослабленного вихря цель теплее
    y = 0.5 * np.where((u10 < SSW_Z) | (u10_l < SSW_Z), 1.0, -0.3) + rng.normal(0, 0.7, n)
    edges = np.column_stack([np.full(n, -1.0), np.full(n, 1.0)])
    X = pd.DataFrame({"u10_a": u10, "u10_a_l1": u10_l, "z50_a": rng.normal(0, 1, n), "lead": np.full(n, 0.25)})
    m = SSWModel()
    m.fit(X, y, edges=edges, years=np.arange(1990, 1990 + n) % 100 + 1990)
    for weak in (SSW_Z - 0.5, 0.8):
        x = pd.Series({"u10_a": weak, "u10_a_l1": 0.8, "z50_a": 0.0, "lead": 0.25})
        p, q = m.predict(x, -1.0, 1.0)
        assert abs(p.sum() - 1.0) < 1e-6
        assert (p > 0).all()
        assert (np.diff(q) >= -1e-9).all()
    # ослабленный вихрь должен давать больше вероятности на тёплый терциль
    p_warm, _ = m.predict(pd.Series({"u10_a": -1.5, "u10_a_l1": -1.5, "z50_a": 0.0, "lead": 0.25}), -1.0, 1.0)
    p_cold, _ = m.predict(pd.Series({"u10_a": 0.9, "u10_a_l1": 0.9, "z50_a": 0.0, "lead": 0.25}), -1.0, 1.0)
    assert p_warm[2] > p_cold[2]
