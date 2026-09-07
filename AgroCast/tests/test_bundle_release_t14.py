import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from agrocast.bundle.releases import (
    BundleError,
    active_release,
    build_release,
    publish,
    release_status,
    rollback,
    set_active,
    verify_release,
)


def make_world(root, marker="v1"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "ready.json").write_text(json.dumps({"ok": True, "contract": "world-v1", "predictor_through": "2026-08-01", "marker": marker}))
    (root / "config.json").write_text(json.dumps({"region": "krai", "marker": marker}))
    (root / "artifacts").mkdir(exist_ok=True)
    (root / "artifacts" / "krai_grid_skill.json").write_text(json.dumps({"schema": "grid-skill-v2", "combos": {}}))
    return root


def test_build_verify_activate_rollback(tmp_path):
    world = make_world(tmp_path / "world", "v1")
    root = tmp_path / "bundles"
    built = build_release(world, root)
    assert built["reused"] is False and built["files"] == 3
    assert not verify_release(tmp_path / "bundles" / built["release_id"], full=True)
    second = build_release(world, root)
    assert second["release_id"] == built["release_id"] and second["reused"] is True
    assert len([p for p in root.iterdir() if p.is_dir()]) == 1

    set_active(root, built["release_id"])
    active = active_release(root)
    assert active["release_id"] == built["release_id"] and not active["problems"]
    assert active["path"] == str(root / built["release_id"])

    (root / built["release_id"] / "config.json").write_text('{"tampered": true}')
    broken = active_release(root)
    assert broken["path"] is None and any(("checksum" in p or "size mismatch" in p) for p in broken["problems"])
    status = release_status(root)
    assert status["status"] == "corrupt"

    world2 = make_world(tmp_path / "world2", "v2")
    res = publish(world2, root)
    assert res["release_id"] != built["release_id"]
    with pytest.raises(BundleError, match="no earlier valid release"):
        rollback(root)
    assert active_release(root)["release_id"] == res["release_id"]


def test_rollback_chain_walks_back_through_valid_releases(tmp_path):
    root = tmp_path / "bundles"
    ids = []
    for i in range(3):
        res = publish(make_world(tmp_path / f"w{i}", f"r{i}"), root)
        ids.append(res["release_id"])
    assert active_release(root)["release_id"] == ids[2]
    rolled = rollback(root)
    assert rolled["release_id"] == ids[1]
    rolled = rollback(root)
    assert rolled["release_id"] == ids[2]
    rolled = rollback(root)
    assert rolled["release_id"] == ids[1]
    (root / ids[1] / "config.json").write_text("x" * 40)
    with pytest.raises(BundleError, match="corrupt"):
        set_active(root, ids[1])
    from agrocast.store.atomic import write_json

    write_json(root / "active.json", {"schema": "bundle-active-v1", "release_id": ids[1], "activated_utc": "2026-09-07T00:00:00Z"})
    assert active_release(root)["problems"]
    rolled = rollback(root)
    assert rolled["release_id"] == ids[2]
    assert not active_release(root)["problems"]
    assert not active_release(root)["problems"]


def test_activate_rejects_corrupt_and_missing(tmp_path):
    world = make_world(tmp_path / "world")
    root = tmp_path / "bundles"
    built = build_release(world, root)
    path = root / built["release_id"] / "ready.json"
    path.write_text('{"ok": false}')
    with pytest.raises(BundleError, match="corrupt"):
        set_active(root, built["release_id"])
    with pytest.raises(BundleError, match="does not exist"):
        set_active(root, "no-such-release")


def test_staging_leftovers_never_activate(tmp_path):
    world = make_world(tmp_path / "world")
    root = tmp_path / "bundles"
    built = build_release(world, root)
    staging = [p for p in root.iterdir() if p.name.startswith(".staging")]
    assert not staging
    assert built["release_id"] in [p.name for p in root.iterdir() if p.is_dir()]


def test_concurrent_reader_sees_whole_releases(tmp_path):
    root = tmp_path / "bundles"
    ids = []
    for i in range(6):
        res = publish(make_world(tmp_path / f"w{i}", f"m{i}"), root)
        ids.append(res["release_id"])
        time.sleep(0.01)
    seen = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            info = active_release(root)
            if info is not None:
                seen.append((info["release_id"], bool(info["problems"])))

    initial = publish(make_world(tmp_path / "w6", "m6"), root)
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(7, 11):
            publish(make_world(tmp_path / f"w{i}", f"m{i}"), root)
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert seen
    assert not any(problems for _, problems in seen)
    all_ids = set(ids) | {initial["release_id"]} | {p.name for p in root.iterdir() if p.is_dir()}
    assert all(rid in all_ids for rid, _ in seen)


def test_desktop_releases_follow_bundle_version(tmp_path, monkeypatch):
    from agrocast.core.settings import RuntimeSettings
    from agrocast.serve import local

    state = tmp_path / "state"
    state.mkdir()
    world = make_world(tmp_path / "bundled", "seed")
    settings = RuntimeSettings(world_dir=world, state_dir=state, bundles_dir=tmp_path / "bundles")
    first = local.ensure_releases(settings)
    again = local.ensure_releases(settings)
    assert first.data_release == again.data_release
    res = publish(make_world(tmp_path / "fresh", "new"), settings.bundles_dir)
    active_settings, info = local.use_active_bundle(settings)
    assert info["status"] == "ok" and active_settings.world_dir == tmp_path / "bundles" / res["release_id"]
    second = local.ensure_releases(active_settings)
    assert second.data_release != first.data_release
    (tmp_path / "bundles" / res["release_id"] / "ready.json").write_text("garbage")
    with pytest.raises(ValueError, match="corrupt"):
        local.use_active_bundle(settings)


def test_release_manifest_digest_binds_cache_identity(tmp_path):
    from agrocast.serve.local import current_releases_manifest
    from agrocast.store.results import Releases

    root = tmp_path / "bundles"
    res_a = publish(make_world(tmp_path / "wa", "A"), root)
    res_b = publish(make_world(tmp_path / "wb", "B"), root)
    manifest_a = current_releases_manifest(SimpleNamespace(world_dir=root / res_a["release_id"]))
    manifest_b = current_releases_manifest(SimpleNamespace(world_dir=root / res_b["release_id"]))
    rel_a = Releases.model_validate(manifest_a)
    rel_b = Releases.model_validate(manifest_b)
    assert rel_a.data_release != rel_b.data_release
    assert rel_a.model_release != rel_b.model_release
    assert rel_a.application_release == rel_b.application_release
    manifest_again = current_releases_manifest(SimpleNamespace(world_dir=root / res_a["release_id"]))
    assert Releases.model_validate(manifest_again) == rel_a


def test_zarr_store_swap_survives_failed_write(tmp_path, monkeypatch):
    from agrocast.store.zarrstore import ZarrStore

    store = ZarrStore(tmp_path / "zarr")
    idx = pd.date_range("2026-01-01", periods=4)
    good = xr.Dataset({"tp": (("time",), np.array([1.0, 2.0, 3.0, 4.0]))}, coords={"time": idx})
    store.write("daily_region", good)
    assert np.allclose(store.open("daily_region")["tp"].values, [1.0, 2.0, 3.0, 4.0])

    def boom(self, *args, **kwargs):
        raise OSError("interrupted download")

    monkeypatch.setattr(xr.Dataset, "to_zarr", boom)
    with pytest.raises(OSError):
        store.write("daily_region", xr.Dataset({"tp": (("time",), np.array([0.0, 0.0, 0.0, 0.0]))}, coords={"time": idx}))
    assert np.allclose(store.open("daily_region")["tp"].values, [1.0, 2.0, 3.0, 4.0])
    assert not (store.path("daily_region").parent / "daily_region.old").exists()
    monkeypatch.undo()
    updated = xr.Dataset({"tp": (("time",), np.array([9.0, 9.0, 9.0, 9.0]))}, coords={"time": idx})
    store.write("daily_region", updated)
    assert np.allclose(store.open("daily_region")["tp"].values, [9.0, 9.0, 9.0, 9.0])
    assert not (store.path("daily_region").parent / "daily_region.old").exists()


def test_readiness_reports_bundle_release(tmp_path, monkeypatch):
    settings = SimpleNamespace(bundles_dir=tmp_path / "bundles", world_dir=make_world(tmp_path / "world"))
    assert release_status(settings.bundles_dir)["status"] == "none"
    res = publish(settings.world_dir, settings.bundles_dir)
    status = release_status(settings.bundles_dir)
    assert status["status"] == "ok" and status["release_id"] == res["release_id"]
    (tmp_path / "bundles" / res["release_id"] / "config.json").write_text("broken{")
    assert release_status(settings.bundles_dir)["status"] == "corrupt"
