import os

import pytest

from agrocast.serve import region as region_mod
from agrocast.serve.product import (RegionRefreshRequest, region_grid, region_refresh, region_regions, region_skill)
from agrocast.serve.errors import APIError


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


def test_invalid_region_models_and_disabled_refresh():
    for fn in (region_grid, region_skill):
        with pytest.raises(ValueError):
            fn(region="bavaria")
    with pytest.raises(ValueError):
        RegionRefreshRequest(start="2026-10", region="bavaria")
    with pytest.raises(APIError) as error:
        region_refresh(RegionRefreshRequest(start="2026-10"))
    assert error.value.status == 403


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


def test_regions_endpoint_lists_only_pilot_region():
    out = region_regions()
    assert out["ok"] is True
    rows = {r["region"]: r for r in out["regions"]}
    assert set(rows) == {"krai"}
    assert out["validation"]["status"] == "unverified"
    assert rows["krai"]["built"] is True and rows["krai"]["n_cells"] == 28
    assert rows["krai"]["name"] == "Краснодарский край"
    assert rows["krai"]["bounds"]["lat_min"] == 44.0
    assert rows["krai"]["skill"] is True


def test_field_paths_per_region(tmp_path):
    p_krai = region_mod.legacy_field_path(tmp_path, "krai")
    assert str(p_krai).endswith(os.path.join("audit", "krig_demo_tp.json"))
    p_st = region_mod.legacy_field_path(tmp_path, "stavropol")
    assert str(p_st).endswith(os.path.join("audit", "stavropol_field.json"))
    p_ro = region_mod.legacy_field_path(tmp_path, "rostov")
    assert str(p_ro).endswith(os.path.join("audit", "rostov_field.json"))
    assert p_st != p_krai and p_ro != p_st


def test_region_block_per_region(tmp_path):
    from agrocast.core.settings import RuntimeSettings

    base = RuntimeSettings.from_environment().world_dir
    txt_k = region_mod.region_block(str(base), str(tmp_path), "krai")
    assert "Краснодарский край" in txt_k
    assert "поле региона не рассчитано" in txt_k
    txt_s = region_mod.region_block(str(base), str(tmp_path), "stavropol")
    assert "Ставропольский край" in txt_s and "сетка: 8 ячеек" in txt_s
    txt_r = region_mod.region_block(str(base), str(tmp_path), "rostov")
    assert "Ростовская область" in txt_r and "сетка: 17 ячеек" in txt_r
    assert region_mod.region_block(str(base), str(tmp_path), "bavaria") == ""
