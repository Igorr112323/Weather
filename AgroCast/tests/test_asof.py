import numpy as np
import pandas as pd
import pytest

from agrocast.core.config import Config
from agrocast.features.dataset import training_data
from agrocast.forecast.asof import (EVAL_PROSPECTIVE, EVAL_REPLAY_REVISED, effective_cutoff, evaluation_mode,
                                    months_of_delay, release_context, target_allowed, target_end_periods,
                                    validate_context, vintages_available, walk_forward_masks)


def test_months_of_delay_boundaries():
    assert months_of_delay(0) == 0
    assert months_of_delay(5) == 1
    assert months_of_delay(31) == 1
    assert months_of_delay(32) == 2
    assert months_of_delay(-3) == 0
    assert effective_cutoff(pd.Period("2025-12", "M"), 5) == pd.Period("2025-11", "M")


def test_release_context_fields_and_validation(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "state"), publication_delay_days=5)
    ctx = release_context(cfg, pd.Period("2025-12", "M"), sources={"sst": "2025-10"})
    assert ctx["schema"] == "asof-v1"
    assert ctx["issue_month"] == "2025-12"
    assert ctx["observation_cutoff"] == "2025-12"
    assert ctx["train_cutoff"] == "2025-11"
    assert ctx["sources"] == {"sst": "2025-10"}
    assert ctx["evaluation"] == EVAL_REPLAY_REVISED
    assert validate_context(ctx)
    bad = dict(ctx, observation_cutoff="2025-09")
    with pytest.raises(ValueError):
        validate_context(bad)
    stale = dict(ctx, sources={"sst": "2026-01"})
    with pytest.raises(ValueError):
        validate_context(stale)


def test_target_allowed_window_and_delay():
    cutoff = pd.Period("2025-11", "M")
    assert target_allowed(pd.Period("2025-09", "M"), 3, cutoff)
    assert not target_allowed(pd.Period("2025-10", "M"), 3, cutoff)
    assert not target_allowed(pd.Period("2025-09", "M"), 3, cutoff, embargo=1)


def test_vintages_probe_switches_evaluation_mode(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "state"), publication_delay_days=5)
    assert evaluation_mode(cfg, pd.Period("2025-12", "M")) == EVAL_REPLAY_REVISED
    vdir = tmp_path / "vintages" / "2025-12"
    vdir.mkdir(parents=True)
    (vdir / "manifest.json").write_text("{}")
    cfg2 = Config(data_dir=str(tmp_path / "state"), publication_delay_days=5, vintages_dir=str(tmp_path / "vintages"))
    assert vintages_available(cfg2, pd.Period("2025-12", "M")) is True
    assert evaluation_mode(cfg2, pd.Period("2025-12", "M")) == EVAL_PROSPECTIVE
    assert evaluation_mode(cfg2, pd.Period("2026-01", "M")) == EVAL_REPLAY_REVISED


def _toy_frames(n_years=24, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.period_range("2000-01", periods=n_years * 12, freq="M")
    pf = pd.DataFrame(rng.normal(size=(len(idx), 3)), index=idx, columns=["a", "b", "c"])
    std = pd.DataFrame(
        {
            "z": rng.normal(size=len(idx)),
            "mu": np.zeros(len(idx)),
            "sd": np.ones(len(idx)),
            "e1": np.full(len(idx), -0.5),
            "e2": np.full(len(idx), 0.5),
        },
        index=idx,
    )
    return pf, std


def test_seasonal_target_window_purged_from_train():
    pf, std = _toy_frames()
    cols = ["a", "b", "c"]
    cutoff = pd.Period("2020-11", "M")
    X, y, meta = training_data(pf, std, "z", 10, 1, until=cutoff, use_cols=cols, span=1)
    assert len(X) > 0
    starts_span1 = meta["period"]
    assert (starts_span1 <= cutoff).all()
    X3, y3, meta3 = training_data(pf, std, "z", 10, 1, until=cutoff, use_cols=cols, span=3)
    assert (meta3["period"] + 2 <= cutoff).all()
    assert len(X3) < len(X)
    Xe, ye, metae = training_data(pf, std, "z", 10, 1, until=cutoff, use_cols=cols, span=3, embargo=1)
    assert (metae["period"] + 3 <= cutoff).all()
    assert len(Xe) <= len(X3)
    Xdec, _, metadec = training_data(pf, std, "z", 12, 1, until=pd.Period("2020-11", "M"), use_cols=cols, span=3)
    for period in metadec["period"]:
        end = period + 2
        assert end.year < 2021 or (end.year == 2020 and end.month <= 11) or end > period


def test_walk_forward_masks_exclude_future_and_straddling_seasons():
    rec = pd.DataFrame(
        {
            "year": [2019, 2019, 2020, 2020, 2021, 2021],
            "target_month": [3, 11, 2, 6, 1, 7],
        }
    )
    ends = target_end_periods(rec, 3)
    masks = walk_forward_masks(ends, rec["year"].unique(), embargo=2)
    assert bool(masks[2020][0])
    assert not bool(masks[2020][1])
    assert not bool(masks[2020][2])
    assert not bool(masks[2020][4])
    for i in np.flatnonzero(masks[2021]):
        assert ends[int(i)] <= pd.Period("2021-01", "M") - 1 - 2
    assert bool(masks[2021][1])
    assert not bool(masks[2021][4])


def test_ledger_folds_only_use_past_records():
    from agrocast.skill.ledger import build_ledger

    rng = np.random.default_rng(11)
    rows = []
    for y in range(2015, 2023):
        for sm in (3, 6, 9, 12):
            for lead in (1, 2):
                p = rng.dirichlet([1.1, 1.1, 1.1])
                obs = int(rng.integers(0, 3))
                for v in ("t2m",):
                    for model in ("ridge", "analog", "clim"):
                        rows.append(
                            {
                                "variable": v,
                                "start_month": sm,
                                "lead": lead,
                                "target_month": int(((sm + lead - 2) % 12) + 1),
                                "year": y + 1 if sm + lead - 1 > 12 else y,
                                "model": model,
                                "p0": float(p[0]),
                                "p1": float(p[1]),
                                "p2": float(p[2]),
                                "q10": -1.0,
                                "q50": 0.0,
                                "q90": 1.0,
                                "obs_z": float(rng.normal()),
                                "obs_tercile": obs,
                                "mu": 0.0,
                                "sd": 1.0,
                            }
                        )
    records = pd.DataFrame(rows)
    led = build_ledger(records, mode="monthly", config=None)
    assert not led.empty
    assert "fold_train_until" in led.columns
    first_year = records["year"].min()
    for _, r in led.iterrows():
        if int(r["year"]) == int(first_year):
            assert r["fold_train_until"] == ""
            continue
        fold_limit = pd.Period(f"{int(r['year'])}-01", "M") - 1
        assert pd.Period(r["fold_train_until"], "M") <= fold_limit


@pytest.fixture(scope="module")
def synth_pair(tmp_path_factory):
    import xarray as xr

    from agrocast.ingest.synthetic import build_synthetic

    root = tmp_path_factory.mktemp("asof-synth")
    cfg_full = Config(data_dir=str(root / "full"), random_state=7, publication_delay_days=5)
    build_synthetic(cfg_full, end=pd.Timestamp("2021-12-31"))
    cfg_trunc = Config(data_dir=str(root / "trunc"), random_state=7, publication_delay_days=5)
    cut = pd.Period("2021-06", "M")
    for name in ("sst", "fields_monthly", "daily_region"):
        ds = xr.open_zarr(str(cfg_full.zarr_dir / name))
        mask = ds.time.to_index().to_period("M") <= cut
        ds.isel(time=slice(0, int(mask.sum()))).to_zarr(str(cfg_trunc.zarr_dir / name))
    return cfg_full, cfg_trunc


def test_future_data_cannot_move_earlier_predictions(synth_pair):
    from agrocast.backtest.engine import run_backtest

    cfg_full, cfg_trunc = synth_pair
    kwargs = dict(variables=("t2m",), start_months=[3], leads=[1, 2], years=[2020], mode="monthly", save_artifacts=False)
    rec_full = run_backtest(cfg_full, **kwargs)
    rec_trunc = run_backtest(cfg_trunc, **kwargs)
    assert not rec_full.empty
    pd.testing.assert_frame_equal(rec_full, rec_trunc, check_exact=True)


def test_backtest_records_carry_verifiable_intervals(synth_pair):
    from agrocast.backtest.engine import run_backtest

    cfg_full, _ = synth_pair
    rec = run_backtest(cfg_full, variables=("t2m",), start_months=[3], leads=[1], years=[2020], mode="monthly", save_artifacts=False)
    assert not rec.empty
    for col in ("issue", "observation_cutoff", "train_until", "train_n", "target_start", "target_end", "evaluation"):
        assert col in rec.columns
    for _, r in rec.iterrows():
        issue = pd.Period(r["issue"], "M")
        assert pd.Period(r["observation_cutoff"], "M") == issue - months_of_delay(5)
        assert pd.Period(r["train_until"], "M") == pd.Period(r["observation_cutoff"], "M")
        assert pd.Period(r["target_start"], "M") > issue
        assert pd.Period(r["target_end"], "M") >= pd.Period(r["target_start"], "M")
        assert int(r["train_n"]) >= 18
        assert r["evaluation"] == EVAL_REPLAY_REVISED
