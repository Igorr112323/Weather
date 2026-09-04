import json
import os
import time

import pytest

from agrocast.serve import region as region_mod
from agrocast.serve.product import JOBS, RegionRefreshRequest, region_field, region_grid, region_refresh, region_skill


def test_grid_endpoint_serves_28_cells():
    out = region_grid()
    assert out["ok"] is True
    cells = out["grid"]["cells"]
    assert len(cells) == 28
    assert [c["id"] for c in cells] == [f"P{i:02d}" for i in range(1, 29)]
    b = out["grid"]["bounds"]
    assert b == {"lat_min": 44.0, "lat_max": 46.5, "lon_min": 37.0, "lon_max": 40.5}
    assert all(c["coverage"] >= 0.9 for c in cells)


def test_skill_endpoint_serves_audit_numbers():
    out = region_skill()
    assert out["ok"] is True
    s = out["skill"]
    assert s["n_points"] == 28
    assert s["verifications"] == 26880
    assert 0.25 <= s["seasonal_t2m"]["rpss"] <= 0.28
    assert 0.60 <= s["seasonal_t2m"]["hit"] <= 0.68
    assert s["seasonal_tp"]["rpss"] < 0
    assert set(s["seasonal_t2m"]["seasons"]) == {"DJF", "MAM", "JJA", "SON"}
    assert len(s["by_point"]) == 28


def test_field_endpoint_missing_stale_fresh(tmp_path, monkeypatch):
    import agrocast.serve.product as product_mod

    monkeypatch.setattr(product_mod, "DATA_ROOT", str(tmp_path))
    out = region_field()
    assert out["ok"] is False

    fp = region_mod.field_path(tmp_path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"months": ["2026-10", "2026-11", "2026-12"], "issue_through": "2026-02"},
               "points": [], "field": {"below": [[0.33]], "normal": [[0.33]], "above": [[0.34]]}}
    fp.write_text(json.dumps(payload), encoding="utf-8")
    fresh = region_field()
    assert fresh["ok"] is True
    assert fresh["meta"]["stale"] is False
    assert fresh["field"]["below"] == [[0.33]]

    old = time.time() - region_mod.FIELD_MAX_AGE_S - 3600
    os.utime(fp, (old, old))
    stale = region_field()
    assert stale["ok"] is True
    assert stale["meta"]["stale"] is True


def test_refresh_endpoint_runs_job_and_field_appears(tmp_path, monkeypatch):
    import agrocast.serve.product as product_mod

    monkeypatch.setattr(product_mod, "DATA_ROOT", str(tmp_path))
    calls = []

    def stub_build(start, world_dir, data_root, workers=2, log=None):
        calls.append(start)
        fp = region_mod.field_path(data_root)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({"meta": {"months": [start], "issue_through": "2026-02"},
                                  "points": [], "field": {"below": [[1.0]], "normal": [[0.0]], "above": [[0.0]]}}),
                      encoding="utf-8")
        if log:
            log("stub done")
        return fp

    monkeypatch.setattr(region_mod, "build_field", stub_build)
    r = region_refresh(RegionRefreshRequest(start="2026-10"))
    assert r["ok"] is True and r["job"]
    job = JOBS[r["job"]]
    for _ in range(100):
        if job.status != "running":
            break
        time.sleep(0.05)
    assert job.status == "done"
    assert calls == ["2026-10"]
    out = region_field()
    assert out["ok"] is True
    assert out["meta"]["months"] == ["2026-10"]
