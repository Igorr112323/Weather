import numpy as np
import pandas as pd

from agrocast.models.deep_analog import DeepAnalogModel, DEEP_COLS


def test_deep_analog_fit_predict():
    rng = np.random.default_rng(2)
    n = 140
    cols = [c for c in DEEP_COLS][:8]
    X = pd.DataFrame({c: rng.normal(0, 1, n) for c in cols})
    X["lead"] = 0.25
    # «правда» зависит от 3 признаков (остальные — шум)
    y = 1.2 * X[cols[0]] - 0.8 * X[cols[2]] + 0.6 * X[cols[4]] + rng.normal(0, 0.6, n)
    edges = np.column_stack([np.full(n, -1.0), np.full(n, 1.0)])
    m = DeepAnalogModel(k=10, n_pcs=4)
    m.fit(X, y, edges=edges, years=np.arange(1985, 1985 + n) % 100 + 1985)
    x = pd.Series(X.iloc[-1].to_numpy(), index=list(X.columns))
    p, q = m.predict(x, -1.0, 1.0)
    assert abs(p.sum() - 1.0) < 1e-6
    assert (p > 0).all()
    assert (np.diff(q) >= -1e-9).all()
    an = m.analogs(x, k=5)
    assert len(an) == 5
    assert all(0 < a["weight"] <= 1 for a in an)


def test_deep_analog_raises_without_features():
    X = pd.DataFrame({"a": [0.0, 1.0], "lead": [0.25, 0.25]})
    m = DeepAnalogModel()
    m.fit(X, [0.0, 1.0])
    x = pd.Series([0.0, 0.25], index=["a", "lead"])
    try:
        m.predict(x, -1.0, 1.0)
        assert False, "ожидалось исключение"
    except RuntimeError:
        pass
