import json
import os
import time

import pytest

from agrocast.serve import region as region_mod
from agrocast.serve.product import (JOBS, RegionRefreshRequest, region_field, region_grid,
                                    region_refresh, region_regions, region_skill)


def test_grid_endpoint_serves_28_cells():
    out = region_grid()
    assert out["ok"] is True
    cells = out["grid"]["cells"]
    assert len(cells) == 28
    assert [c["id"] for c in cells] == [f"P{i:02d}" for i in range(1, 29)]
    b = out["grid"]["bounds"]
    assert b == {"lat_min": 44.0, "lat_max": 46.5, "lon_min": 37.0, "lon_max": 40.5}
    assert all(c["coverage"] >= 0.9 for c in cells)
    assert out["grid"]["region"] == "krai"


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
    assert fresh["meta"]["region"] == "krai"
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

    def stub_build(start, world_dir, data_root, region="krai", workers=2, log=None):
        calls.append(start)
        fp = region_mod.field_path(data_root, region)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({"meta": {"months": [start], "issue_through": "2026-02",
                                           "region": region},
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


def test_unknown_region_returns_400():
    for fn in (region_grid, region_skill, region_field):
        r = fn(region="bavaria")
        assert r.status_code == 400
    r = region_refresh(RegionRefreshRequest(start="2026-10", region="bavaria"))
    assert r.status_code == 400
    ok = region_grid(region="krai")
    assert ok["ok"] is True


def test_stavropol_grid_serves_8_cells():
    out = region_grid(region="stavropol")
    assert out["ok"] is True
    g = out["grid"]
    cells = g["cells"]
    assert len(cells) == 8
    assert [c["id"] for c in cells] == [f"P{i:02d}" for i in range(1, 9)]
    assert g["bounds"] == {"lat_min": 44.8, "lat_max": 46.2, "lon_min": 40.5, "lon_max": 43.0}
    assert g["n_candidates"] == 10 and g["n_cells"] == 8
    assert all(c["coverage"] >= 0.9 for c in cells)
    assert g["region"] == "stavropol"


def test_stavropol_skill_serves_audit_numbers():
    out = region_skill(region="stavropol")
    assert out["ok"] is True
    s = out["skill"]
    assert s["n_points"] == 8
    assert s["verifications"] == 7680
    assert s["region"] == "stavropol"
    assert s["seasonal_t2m"]["rpss"] > 0.15
    assert s["seasonal_t2m"]["hit"] > 0.5
    assert s["seasonal_tp"]["rpss"] < 0
    assert len(s["by_point"]) == 8
    assert all(p["seasonal_t2m_rpss"] > 0 for p in s["by_point"])


def test_regions_endpoint_lists_registry_with_built_flags():
    out = region_regions()
    assert out["ok"] is True
    rows = {r["region"]: r for r in out["regions"]}
    assert set(rows) == {"krai", "stavropol", "rostov"}
    assert rows["krai"]["built"] is True and rows["krai"]["n_cells"] == 28
    assert rows["stavropol"]["built"] is True and rows["stavropol"]["n_cells"] == 8
    assert rows["rostov"]["built"] is True and rows["rostov"]["n_cells"] == 17
    assert rows["krai"]["name"] == "Краснодарский край"
    assert rows["krai"]["bounds"]["lat_min"] == 44.0
    assert rows["krai"]["skill"] is True
    assert rows["stavropol"]["seasonal_t2m_rpss"] > 0.2
    assert rows["rostov"]["verifications"] == 16320
    assert rows["rostov"]["seasonal_t2m_rpss"] > 0.2


def test_field_paths_per_region(tmp_path):
    p_krai = region_mod.field_path(tmp_path, "krai")
    assert str(p_krai).endswith(os.path.join("audit", "krig_demo_tp.json"))
    p_st = region_mod.field_path(tmp_path, "stavropol")
    assert str(p_st).endswith(os.path.join("audit", "stavropol_field.json"))
    p_ro = region_mod.field_path(tmp_path, "rostov")
    assert str(p_ro).endswith(os.path.join("audit", "rostov_field.json"))
    assert p_st != p_krai and p_ro != p_st


def test_refresh_per_region_runs_job_with_region(tmp_path, monkeypatch):
    import agrocast.serve.product as product_mod

    monkeypatch.setattr(product_mod, "DATA_ROOT", str(tmp_path))
    calls = []

    def stub_build(start, world_dir, data_root, region="krai", workers=2, log=None):
        calls.append((start, region))
        fp = region_mod.field_path(data_root, region)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({"meta": {"months": [start], "issue_through": "2026-02",
                                           "region": region},
                                  "points": [], "field": {"below": [[1.0]], "normal": [[0.0]], "above": [[0.0]]}}),
                      encoding="utf-8")
        if log:
            log("stub done")
        return fp

    monkeypatch.setattr(region_mod, "build_field", stub_build)
    r = region_refresh(RegionRefreshRequest(start="2026-11", region="stavropol"))
    assert r["ok"] is True and r["job"] and r["region"] == "stavropol"
    job = JOBS[r["job"]]
    for _ in range(100):
        if job.status != "running":
            break
        time.sleep(0.05)
    assert job.status == "done"
    assert calls == [("2026-11", "stavropol")]
    out = region_field(region="stavropol")
    assert out["ok"] is True
    assert out["meta"]["region"] == "stavropol"
    assert out["meta"]["months"] == ["2026-11"]
    assert region_mod.field_path(tmp_path, "stavropol").exists()
    miss = region_field(region="rostov")
    assert miss["ok"] is False


def test_region_block_per_region(tmp_path):
    import agrocast.serve.product as product_mod
    from pathlib import Path

    base = Path(product_mod.WORLD)
    txt_k = region_mod.region_block(str(base), str(tmp_path), "krai")
    assert "Краснодарский край" in txt_k
    assert "поле региона не рассчитано" in txt_k
    txt_s = region_mod.region_block(str(base), str(tmp_path), "stavropol")
    assert "Ставропольский край" in txt_s and "сетка: 8 ячеек" in txt_s
    txt_r = region_mod.region_block(str(base), str(tmp_path), "rostov")
    assert "Ростовская область" in txt_r and "сетка: 17 ячеек" in txt_r
    assert region_mod.region_block(str(base), str(tmp_path), "bavaria") == ""
