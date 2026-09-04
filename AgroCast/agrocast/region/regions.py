from __future__ import annotations

import json
from pathlib import Path

from agrocast.region.grid import CELL_DEG, MIN_COVERAGE, krai_cells, save_grid

REGIONS = {
    "krai": {"name": "Краснодарский край", "bounds": (44.0, 46.5, 37.0, 40.5)},
    "stavropol": {"name": "Ставропольский край", "bounds": (44.8, 46.2, 40.5, 43.0)},
    "rostov": {"name": "Ростовская область", "bounds": (46.0, 47.5, 38.0, 43.0)},
}

DEFAULT_WORLD = Path(__file__).resolve().parents[2] / "world"


def known(region):
    return region in REGIONS


def region_name(region):
    return REGIONS[region]["name"]


def region_bounds(region="krai"):
    return REGIONS[region]["bounds"]


def grid_artifact_path(world_dir, region="krai"):
    return Path(world_dir) / "artifacts" / f"{region}_grid.json"


def skill_artifact_path(world_dir, region="krai"):
    return Path(world_dir) / "artifacts" / f"{region}_grid_skill.json"


def region_cells(store, region="krai", min_coverage=MIN_COVERAGE, cell=CELL_DEG):
    return krai_cells(store, min_coverage=min_coverage, cell=cell,
                      bounds=region_bounds(region))


def save_region_grid(cells, world_dir=DEFAULT_WORLD, region="krai",
                     min_coverage=MIN_COVERAGE, cell=CELL_DEG):
    path = grid_artifact_path(world_dir, region)
    return save_grid(cells, path=path, bounds=region_bounds(region), cell=cell,
                     min_coverage=min_coverage, name=f"{region}_grid")


def load_region_grid(world_dir=DEFAULT_WORLD, region="krai"):
    return json.loads(grid_artifact_path(world_dir, region).read_text(encoding="utf-8"))


def load_region_skill(world_dir=DEFAULT_WORLD, region="krai"):
    p = skill_artifact_path(world_dir, region)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def region_summary(world_dir=DEFAULT_WORLD):
    world_dir = Path(world_dir)
    rows = []
    for rid, meta in REGIONS.items():
        gp = grid_artifact_path(world_dir, rid)
        row = {
            "region": rid,
            "name": meta["name"],
            "bounds": dict(zip(("lat_min", "lat_max", "lon_min", "lon_max"), meta["bounds"])),
            "built": gp.exists(),
        }
        if gp.exists():
            g = json.loads(gp.read_text(encoding="utf-8"))
            row["n_candidates"] = int(g["n_candidates"])
            row["n_cells"] = int(g["n_cells"])
            s = load_region_skill(world_dir, rid)
            if s is not None:
                row["skill"] = True
                row["verifications"] = int(s.get("verifications", 0))
                st = s.get("seasonal_t2m") or {}
                row["seasonal_t2m_rpss"] = st.get("rpss")
                row["seasonal_t2m_hit"] = st.get("hit")
            else:
                row["skill"] = False
        rows.append(row)
    return rows
