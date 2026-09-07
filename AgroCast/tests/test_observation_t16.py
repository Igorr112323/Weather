import numpy as np
import pandas as pd
import pytest

from agrocast.core.observation import (
    FACT_INCOMPLETE,
    FACT_MISSING,
    FACT_NOT_FINITE,
    month_completeness,
    obs_revision,
    window_verdict,
)


def frame(dates, **columns):
    return pd.DataFrame(columns, index=pd.DatetimeIndex(dates))


def test_leap_february_requires_all_29_days():
    full = frame(pd.date_range("2024-02-01", "2024-02-29"), t2m=np.zeros(29), tp=np.zeros(29))
    comp = month_completeness(full)
    assert bool(comp.loc[pd.Period("2024-02", "M"), "complete"])
    cut = full.drop(pd.Timestamp("2024-02-15"))
    comp_cut = month_completeness(cut)
    assert not bool(comp_cut.loc[pd.Period("2024-02", "M"), "complete"])
    assert int(comp_cut.loc[pd.Period("2024-02", "M"), "days_present"]) == 28


def test_non_leap_february_full_with_28_days():
    full = frame(pd.date_range("2023-02-01", "2023-02-28"), t2m=np.zeros(28))
    comp = month_completeness(full, variables=("t2m",))
    assert bool(comp.loc[pd.Period("2023-02", "M"), "complete"])


def test_year_transition_months_assessed_independently():
    dates = pd.date_range("2023-12-25", "2024-01-31")
    full_jan = dates[(dates < "2023-12-31") | (dates >= "2024-01-01")]
    fr = frame(pd.DatetimeIndex(full_jan), t2m=np.zeros(len(full_jan)))
    comp = month_completeness(fr, variables=("t2m",))
    assert not bool(comp.loc[pd.Period("2023-12", "M"), "complete"])
    assert bool(comp.loc[pd.Period("2024-01", "M"), "complete"])
    ok, reason = window_verdict(comp, pd.Period("2023-12", "M"), pd.Period("2023-12", "M"), 0.1, variables=("t2m",))
    assert not ok and reason == FACT_INCOMPLETE
    ok, _ = window_verdict(comp, pd.Period("2024-01", "M"), pd.Period("2024-01", "M"), 0.1, variables=("t2m",))
    assert ok


def test_season_window_spanning_missing_month_is_pending():
    dates = pd.date_range("2020-01-01", "2020-01-31").append(pd.date_range("2020-03-01", "2020-03-31"))
    fr = frame(dates, t2m=np.zeros(len(dates)))
    comp = month_completeness(fr, variables=("t2m",))
    ok, reason = window_verdict(comp, pd.Period("2020-01", "M"), pd.Period("2020-03", "M"), 0.0, variables=("t2m",))
    assert not ok and reason == FACT_MISSING


def test_non_finite_fact_pending_even_when_days_complete():
    dates = pd.date_range("2020-05-01", "2020-05-31")
    comp = month_completeness(frame(dates, t2m=np.zeros(31)), variables=("t2m",))
    ok, reason = window_verdict(comp, pd.Period("2020-05", "M"), pd.Period("2020-05", "M"), float("nan"), variables=("t2m",))
    assert not ok and reason == FACT_NOT_FINITE


def test_variable_scoped_completeness():
    dates = pd.date_range("2020-05-01", "2020-05-31")
    tp = np.zeros(31)
    tp[10] = np.nan
    fr = frame(dates, t2m=np.zeros(31), tp=tp)
    comp = month_completeness(fr, variables=("t2m", "tp"))
    assert not bool(comp.loc[pd.Period("2020-05", "M"), "tp_ok"])
    assert bool(comp.loc[pd.Period("2020-05", "M"), "t2m_ok"])
    ok, _ = window_verdict(comp, pd.Period("2020-05", "M"), pd.Period("2020-05", "M"), 0.0, variables=("t2m",))
    assert ok
    ok, reason = window_verdict(comp, pd.Period("2020-05", "M"), pd.Period("2020-05", "M"), 0.0, variables=("tp",))
    assert not ok and reason == FACT_INCOMPLETE


def test_monthly_tp_all_missing_is_nan_not_zero():
    from agrocast.features.climatology import monthly_from_daily

    dates = pd.date_range("2020-05-01", "2020-05-31")
    tp = np.zeros(31)
    tp[:] = np.nan
    monthly = monthly_from_daily(frame(dates, t2m=np.full(31, 20.0), tp=tp))
    assert np.isnan(monthly.loc[pd.Period("2020-05", "M"), "tp"])


def test_obs_revision_stable_and_sensitive():
    dates = pd.date_range("2019-01-01", "2021-12-31")
    rng = np.random.default_rng(5)
    values = rng.normal(size=len(dates))
    fr = frame(dates, t2m=values)
    rev1 = obs_revision(fr, variables=("t2m",))
    rev2 = obs_revision(fr, variables=("t2m",))
    assert rev1 == rev2
    changed = fr.copy()
    changed.iloc[100, 0] += 0.5
    assert obs_revision(changed, variables=("t2m",)) != rev1


@pytest.fixture()
def synth_config(tmp_path):
    from agrocast.core.config import Config
    from agrocast.ingest.synthetic import build_synthetic

    cfg = Config(data_dir=str(tmp_path), random_state=7, publication_delay_days=0)
    build_synthetic(cfg, end=pd.Timestamp("2021-12-31"))
    return cfg


def test_backtest_excludes_incomplete_fact_month_and_is_idempotent(synth_config, monkeypatch):
    from agrocast.backtest.engine import run_backtest
    import agrocast.features.dataset as dataset_mod

    kwargs = dict(variables=("t2m",), start_months=[3], leads=[1], years=[2020], mode="monthly", save_artifacts=False)
    rec1 = run_backtest(synth_config, **kwargs)
    rec2 = run_backtest(synth_config, **kwargs)
    assert not rec1.empty
    pd.testing.assert_frame_equal(rec1, rec2)
    key = ["variable", "start_month", "lead", "year", "model", "target_start"]
    assert not rec1.duplicated(subset=key).any()
    assert rec1["obs_revision"].nunique() == 1

    original_daily = dataset_mod.PointDataset.daily

    def cut_march_days(self):
        daily = original_daily(self)
        mask = (daily.index >= pd.Timestamp("2020-03-10")) & (daily.index <= pd.Timestamp("2020-03-12"))
        return daily[~mask]

    monkeypatch.setattr(dataset_mod.PointDataset, "daily", cut_march_days)
    rec_cut = run_backtest(synth_config, **kwargs)
    assert rec_cut.empty


def test_backtest_revision_changes_when_observations_revised(synth_config, monkeypatch):
    from agrocast.backtest.engine import run_backtest
    import agrocast.features.dataset as dataset_mod

    kwargs = dict(variables=("t2m",), start_months=[3], leads=[1], years=[2020], mode="monthly", save_artifacts=False)
    base = run_backtest(synth_config, **kwargs)
    original_daily = dataset_mod.PointDataset.daily

    def disturb_far_dates(self):
        daily = original_daily(self).copy()
        mask = daily.index >= pd.Timestamp("2021-01-01")
        daily.loc[mask, "t2m"] = daily.loc[mask, "t2m"] + 0.25
        return daily

    monkeypatch.setattr(dataset_mod.PointDataset, "daily", disturb_far_dates)
    revised = run_backtest(synth_config, **kwargs)
    assert not revised.empty
    pd.testing.assert_frame_equal(base.drop(columns="obs_revision"), revised.drop(columns="obs_revision"))
    assert base["obs_revision"].iloc[0] != revised["obs_revision"].iloc[0]
