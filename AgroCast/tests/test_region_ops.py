import json
import os
import time

import numpy as np

from agrocast.region import grid as grid_mod
from agrocast.region import regions
from agrocast.region.grid import cell_centers, krai_cells
from agrocast.serve import region as region_mod
from agrocast.serve.digest import build_digest
from test_grid import FakeStore, _fake_ds


def test_cell_centers_aligned_on_global_grid():
    lats, lons = cell_centers(bounds=(44.8, 46.2, 40.5, 43.0))
    assert len(lats) == 2 and len(lons) == 5
    assert np.allclose(lats % 0.5, 0.25)
    assert np.allclose(lons % 0.5, 0.25)
    assert np.allclose(lats, [45.25, 45.75])
    assert np.allclose(lons, [40.75, 41.25, 41.75, 42.25, 42.75])
    la0, lo0 = cell_centers()
    assert len(la0) * len(lo0) == 35
    assert abs(la0[0] - 44.25) < 1e-9 and abs(lo0[0] - 37.25) < 1e-9


def test_krai_cells_skip_candidates_missing_in_store():
    lats = [46.75, 46.25]
    lons = [38.25, 38.75, 39.25, 39.75, 40.25, 40.75, 41.25, 41.75, 42.25]
    store = FakeStore(_fake_ds(lats, lons, bad_cell=(0, 0)))
    cells = krai_cells(store, min_coverage=0.9, cell=0.5, bounds=(46.0, 47.5, 38.0, 43.0))
    assert len(cells) == 17
    assert [c["id"] for c in cells] == [f"P{i:02d}" for i in range(1, 18)]
    assert not [c for c in cells if c["lat"] == 47.25 or c["lon"] == 42.75]
    assert all(c["coverage"] >= 0.9 for c in cells)


def test_save_region_grid_and_region_summary(tmp_path):
    cells = [{"id": "P01", "lat": 45.25, "lon": 41.75,
              "coverage_t2m": 0.99, "coverage_tp": 0.99, "coverage": 0.99}]
    art = regions.save_region_grid(cells, world_dir=str(tmp_path), region="stavropol")
    assert (tmp_path / "artifacts" / "stavropol_grid.json").exists()
    assert art["name"] == "stavropol_grid"
    assert art["n_cells"] == 1
    rows = regions.region_summary(str(tmp_path))
    by = {r["region"]: r for r in rows}
    assert set(by) == {"krai", "stavropol", "rostov"}
    assert by["stavropol"]["built"] is True and by["stavropol"]["n_cells"] == 1
    assert by["krai"]["built"] is False
    assert by["rostov"]["bounds"] == {"lat_min": 46.0, "lat_max": 47.5,
                                      "lon_min": 38.0, "lon_max": 43.0}
    assert by["stavropol"]["name"] == "Ставропольский край"


def _repo_world():
    return str(grid_mod.DEFAULT_ARTIFACT.parent.parent)


def test_region_block_reports_field_skill_and_tp(tmp_path):
    import shutil

    src_world = _repo_world()
    for rid in ("krai", "stavropol", "rostov"):
        for name, dstf in (("grid.json", regions.grid_artifact_path),
                           ("grid_skill.json", regions.skill_artifact_path)):
            src = dstf(src_world, rid)
            if not src.exists():
                continue
            dst = dstf(str(tmp_path), rid)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
    fp = region_mod.legacy_field_path(tmp_path, "stavropol")
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"months": ["2026-10", "2026-11", "2026-12"],
                        "issue_through": "2026-08", "dominant_cells": {"below": 3, "normal": 5, "above": 0}},
               "points": [], "field": {}}
    fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    text = region_mod.region_block(str(tmp_path), str(tmp_path), "stavropol")
    assert "Ставропольский край" in text
    assert "2026-10–2026-12" in text
    assert "доминирующая терцель" in text and "below 3" in text
    assert "RPSS" in text
    assert "осадк" in text and "не подтверждён" in text
    missing = region_mod.region_block(str(tmp_path), str(tmp_path), "rostov")
    assert "Ростовская область" in missing
    assert "поле региона не рассчитано" in missing
    assert "сетка: 17 ячеек" in missing


def test_region_freshness_field_and_artifact_statuses(tmp_path):
    from scripts.zarr_freshness import region_freshness

    data_root = tmp_path / "data"
    data_root.mkdir()
    world = _repo_world()
    alerts = region_freshness(world, str(data_root))
    assert not [a for a in alerts if "поле krai" in a]
    assert not [a for a in alerts if "поле stavropol" in a]
    fp = region_mod.legacy_field_path(data_root, "krai")
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps({"meta": {}}), encoding="utf-8")
    fp2 = region_mod.legacy_field_path(data_root, "rostov")
    fp2.parent.mkdir(parents=True, exist_ok=True)
    fp2.write_text(json.dumps({"meta": {}}), encoding="utf-8")
    old = time.time() - 12 * 86400
    os.utime(fp2, (old, old))
    alerts2 = region_freshness(world, str(data_root))
    assert not [a for a in alerts2 if "поле krai" in a]
    assert [a for a in alerts2 if "поле rostov" in a]
    empty_world = tmp_path / "empty_world"
    empty_world.mkdir()
    alerts3 = region_freshness(str(empty_world), str(data_root))
    assert any("krai_grid.json отсутствует" in a for a in alerts3)
    assert any("rostov_grid_skill.json отсутствует" in a for a in alerts3)


def test_build_digest_appends_region_text():
    payload = {"seasons": [{"months": ["2026-10", "2026-11", "2026-12"], "year": 2026}]}
    subject, body = build_digest([], [payload], "Регион · Краснодарский край (krai)\nполе: месяцы 2026-10–2026-12")
    assert "Регион · Краснодарский край (krai)" in body
    assert "2026-10" in body
    assert "поле: месяцы" in body
    assert subject.startswith("AgroCast")
    subject, body = build_digest([], [payload], None)
    assert "Регион · Краснодарский край" not in body
    assert subject.startswith("AgroCast")
