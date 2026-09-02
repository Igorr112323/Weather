import numpy as np
import pandas as pd

from agrocast.skill.ledger import build_ledger, ledger_summary, record_hash


def _fake_records(n_years=8, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for y in range(2015, 2015 + n_years):
        for sm in (3, 6, 9, 12):
            for lead in (1, 2, 3):
                p = rng.dirichlet([1.2, 1.2, 1.2])
                obs = rng.integers(0, 3)
                for v in ("t2m", "tp"):
                    rows.append(
                        {
                            "variable": v,
                            "start_month": sm,
                            "lead": lead,
                            "target_month": (sm + lead - 1) % 12 or 12,
                            "year": y + (1 if (sm + lead - 1) > 12 else 0),
                            "model": "blend",
                            "p0": p[0],
                            "p1": p[1],
                            "p2": p[2],
                            "q10": -1.0,
                            "q50": 0.0,
                            "q90": 1.0,
                            "obs_z": float(rng.normal(0, 1)),
                            "obs_tercile": int(obs),
                        }
                    )
    return pd.DataFrame(rows)


def test_build_ledger_and_summary():
    led = build_ledger(_fake_records())
    assert len(led) > 0
    for col in ("p0", "p1", "p2", "q10", "q50", "q90", "obs_tercile", "rps", "hit", "season", "issue"):
        assert col in led.columns
    s = ledger_summary(led)
    assert s is not None
    for k in ("overall", "t2m", "tp", "by_lead", "by_season", "p80_coverage", "recent"):
        assert k in s
    assert 0 <= s["overall"]["hit"] <= 1
    assert -1 <= s["overall"]["rpss"] <= 1
    assert 0 <= s["p80_coverage"] <= 1
    assert len(s["recent"]) > 0


def test_record_hash_deterministic():
    h1 = record_hash("2026-08", 45.03, 39.07, "t2m", [0.4, 0.3, 0.3], [-1.0, 0.0, 1.0])
    h2 = record_hash("2026-08", 45.03, 39.07, "t2m", [0.4, 0.3, 0.3], [-1.0, 0.0, 1.0])
    h3 = record_hash("2026-08", 45.03, 39.07, "t2m", [0.41, 0.3, 0.29], [-1.0, 0.0, 1.0])
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 16
