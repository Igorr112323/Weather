import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import numpy as np
import pytest
from pydantic import ValidationError

from agrocast.core.config import Config
from agrocast.core.contracts import ForecastSpec, target_months
from agrocast.core.jsoncodec import canonical_json
from agrocast.serve import region
from agrocast.store.results import CacheScope, Releases, ResultCache, ResultIdentity, VarietySnapshot, fingerprint


@pytest.fixture
def releases():
    return Releases(data_release="1" * 64, model_release="2" * 64, application_release="3" * 64)


@pytest.fixture
def request_spec():
    return ForecastSpec(lat=46.25, lon=38.25, point_id="P01", start="2026-10", mode="monthly", horizon=3)


@pytest.fixture
def scope():
    return CacheScope(namespace="owned", owner_id=uuid4(), organization_id=uuid4())




def test_canonical_json_roundtrip_is_idempotent_for_numeric_keys():
    from agrocast.core.jsoncodec import canonical_json, strict_json

    payload = {"by_month": {5: 0.4, 12: 0.7, 1: 0.9}, "tuples": (1, 2.5), "nested": [{2: "b", "20": "a"}]}
    encoded = canonical_json(payload)
    assert canonical_json(strict_json(encoded)) == encoded
    assert canonical_json({"k": {10: 1, 2: 2}}) == canonical_json({"k": {"10": 1, "2": 2}})
    with pytest.raises(ValueError, match="duplicate normalized"):
        canonical_json({1: "int", "1": "str"})


def test_result_cache_hits_payload_with_numeric_keys(tmp_path, request_spec, releases, scope):
    from agrocast.store.results import ResultCache, ResultIdentity

    identity = ResultIdentity.point(request_spec, releases, {"random_state": 7}, scope)
    cache = ResultCache(tmp_path)
    payload = {"frost": {"p_frost_day_by_month": {5: 0.4, 12: 0.7}}, "values": (1.5, 2.5)}
    cache.write(identity, payload)
    hit = cache.read(identity)
    assert hit is not None
    assert hit.payload["frost"] == {"p_frost_day_by_month": {"5": 0.4, "12": 0.7}}


@pytest.mark.parametrize("changes", [
    {"start": "2026-03"}, {"lat": 46.25000001}, {"lon": 38.25000001}, {"region": "rostov"},
    {"point_id": "P02"}, {"horizon": 6}, {"mode": "seasonal", "season_len": 3},
    {"kind": "hindcast", "start": "2024-10"}, {"variables": ["tp"]},
])
def test_every_request_dimension_changes_identity(request_spec, releases, scope, changes):
    original = ResultIdentity.point(request_spec, releases, {"random_state": 0}, scope)
    modified = ForecastSpec.model_validate({**request_spec.model_dump(), **changes})
    other = ResultIdentity.point(modified, releases, {"random_state": 0}, scope)
    assert original.key() != other.key()


@pytest.mark.parametrize("key", ["data_release", "model_release", "application_release"])
def test_release_changes_invalidate_results(request_spec, releases, scope, key):
    first = ResultIdentity.point(request_spec, releases, {}, scope)
    changed = Releases.model_validate({**releases.model_dump(), key: "4" * 64})
    assert ResultIdentity.point(request_spec, changed, {}, scope).key() != first.key()


def test_settings_scope_and_variety_are_part_of_identity(request_spec, releases, scope):
    first = ResultIdentity.point(request_spec, releases, {"random_state": 0}, scope)
    assert first.key() != ResultIdentity.point(request_spec, releases, {"random_state": 1}, scope).key()
    other = CacheScope(namespace="owned", owner_id=uuid4(), organization_id=scope.organization_id)
    assert first.key() != ResultIdentity.point(request_spec, releases, {"random_state": 0}, other).key()
    other_org = CacheScope(namespace="owned", owner_id=scope.owner_id, organization_id=uuid4())
    assert first.key() != ResultIdentity.point(request_spec, releases, {"random_state": 0}, other_org).key()
    variety = VarietySnapshot(id=uuid4(), revision=1, content_sha256=fingerprint({"name": "one"}))
    selected = ForecastSpec.model_validate({**request_spec.model_dump(), "variety_id": variety.id, "variety_revision": 1})
    second = ResultIdentity.point(selected, releases, {"random_state": 0}, scope, variety)
    assert second.key() != first.key()
    revised = VarietySnapshot(id=variety.id, revision=2, content_sha256=fingerprint({"name": "two"}))
    selected = ForecastSpec.model_validate({**selected.model_dump(), "variety_revision": 2})
    third = ResultIdentity.point(selected, releases, {"random_state": 0}, scope, revised)
    assert third.key() != second.key()
    with pytest.raises(ValueError):
        ResultIdentity.point(selected, releases, {}, scope, variety)


def test_canonical_order_and_integer_coordinates_do_not_split_identical_keys(request_spec, releases, scope):
    first = ResultIdentity.point(request_spec, releases, {"b": 2, "a": 1}, scope)
    equivalent = ForecastSpec.model_validate({**request_spec.model_dump(), "variables": ["tp", "t2m"]})
    assert first.key() == ResultIdentity.point(equivalent, releases, {"a": 1, "b": 2}, scope).key()
    positive = ForecastSpec(lat=0, lon=0, start="2026-03")
    negative = ForecastSpec(lat=-0.0, lon=-0.0, start="2026-03")
    assert ResultIdentity.point(positive, releases, {}, scope).key() == ResultIdentity.point(negative, releases, {}, scope).key()


def test_october_and_march_coexist_and_ttl_cannot_hide_mismatch(tmp_path, request_spec, releases, scope):
    clock = [1000]
    cache = ResultCache(tmp_path, clock=lambda: clock[0])
    october = ResultIdentity.point(request_spec, releases, {}, scope)
    march_spec = ForecastSpec.model_validate({**request_spec.model_dump(), "start": "2026-03"})
    march = ResultIdentity.point(march_spec, releases, {}, scope)
    cache.write(october, {"start": "2026-10"})
    assert cache.read(march, max_age=600) is None
    cache.write(march, {"start": "2026-03"})
    assert cache.read(october).payload["start"] == "2026-10"
    assert cache.read(march).payload["start"] == "2026-03"
    cache.path(march).write_bytes(cache.path(october).read_bytes())
    assert cache.read(march) is None
    clock[0] += 601
    os.utime(cache.path(october), None)
    assert cache.read(october, max_age=600) is None
    assert cache.read(october).age_s == 601


@pytest.mark.parametrize("mutation", ["payload", "key", "identity", "incomplete", "future", "broken", "nan"])
def test_corrupt_or_partial_cache_entry_is_never_a_hit(tmp_path, request_spec, releases, scope, mutation):
    cache = ResultCache(tmp_path, clock=lambda: 1000)
    identity = ResultIdentity.point(request_spec, releases, {}, scope)
    path = cache.write(identity, {"value": 1})
    value = json.loads(path.read_text())
    if mutation == "payload":
        value["payload"]["value"] = 2
    elif mutation == "key":
        value["key"] = "0" * 64
    elif mutation == "identity":
        value["identity"]["start"] = "2026-03"
    elif mutation == "incomplete":
        value["state"] = "running"
    elif mutation == "future":
        value["created_at"] = 1001
    path.write_text(json.dumps(value))
    if mutation == "broken":
        path.write_text('{"broken"')
    elif mutation == "nan":
        path.write_text('{"created_at": NaN}')
    assert cache.read(identity) is None


def test_failed_atomic_replacement_does_not_destroy_previous_result(tmp_path, request_spec, releases, scope, monkeypatch):
    cache = ResultCache(tmp_path)
    identity = ResultIdentity.point(request_spec, releases, {}, scope)
    cache.write(identity, {"version": "old"})
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk failure")))
    with pytest.raises(OSError):
        cache.write(identity, {"version": "new"})
    assert cache.read(identity).payload == {"version": "old"}
    assert not list(cache.root.glob("*.tmp"))
    with pytest.raises(ValueError):
        cache.write(identity, {"bad": float("nan")})
    assert cache.read(identity).payload == {"version": "old"}


def test_concurrent_readers_never_observe_half_written_result(tmp_path, request_spec, releases, scope):
    cache = ResultCache(tmp_path)
    identity = ResultIdentity.point(request_spec, releases, {}, scope)
    cache.write(identity, {"version": 0, "data": "x" * 10000})

    def replace(index):
        cache.write(identity, {"version": index, "data": "x" * 10000})
        result = cache.read(identity)
        assert result is not None and len(result.payload["data"]) == 10000

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(replace, range(30)))


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "world"
    Config(data_dir=str(root)).save()
    artifact = root / "artifacts/krai_grid.json"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(json.dumps({"bounds": {"lat_min": 44, "lat_max": 44.5, "lon_min": 38, "lon_max": 38.5}, "cells": [{"id": "P01", "lat": 44.25, "lon": 38.25}]}))
    return root


def test_region_cache_keys_include_grid_configuration_and_release(world, releases):
    first = region.region_identity("2026-10", world, releases=releases)
    assert first != region.region_identity("2026-03", world, releases=releases)
    config = Config.load(world / "config.json")
    config.random_state += 1
    config.save()
    second = region.region_identity("2026-10", world, releases=releases)
    assert first.key() != second.key()
    path = world / "artifacts/krai_grid.json"
    grid = json.loads(path.read_text())
    grid["cells"][0]["lat"] += 0.01
    path.write_text(json.dumps(grid))
    assert second.key() != region.region_identity("2026-10", world, releases=releases).key()


@pytest.mark.parametrize("wrong_period", [False, True])
def test_real_regional_builder_writes_and_reads_parameterized_cache(world, releases, tmp_path, monkeypatch, wrong_period):
    calls = []

    class Executor:
        def __init__(self, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def map(self, function, arguments):
            return map(function, arguments)

    def forecast(args):
        pid, lat, lon, start, _, _, config_snapshot = args
        assert config_snapshot["bundle_dir"] == str(world)
        calls.append(start)
        return {"id": pid, "lat": lat, "lon": lon, "target": start, "months": target_months("2026-11" if wrong_period else start, 3), "issue_through": "2026-02", "below": 0.2, "normal": 0.3, "above": 0.5}

    from agrocast.region import kriging
    monkeypatch.setattr(region, "ProcessPoolExecutor", Executor)
    monkeypatch.setattr(region, "_forecast_cell", forecast)
    monkeypatch.setattr(kriging, "ordinary_kriging", lambda points, values, targets, detrend: SimpleNamespace(pred=np.full(len(targets), values[0]), n_fallback=0, variogram=SimpleNamespace(nugget=0, psill=1, range_km=1)))
    data = tmp_path / "data"
    if wrong_period:
        with pytest.raises(ValueError, match="does not match"):
            region.build_field("2026-10", world, data, releases=releases)
        assert not list(data.glob("results-v1/*.json"))
        return
    october_path = region.build_field("2026-10", world, data, releases=releases)
    march_path = region.build_field("2026-03", world, data, releases=releases)
    assert october_path != march_path
    assert calls == ["2026-10", "2026-03"]
    assert region.build_field("2026-10", world, data, releases=releases) == october_path
    assert calls == ["2026-10", "2026-03"]
    expected = region.region_identity("2026-03", world, releases=releases)
    assert region.field_payload(data, expected)["meta"]["start"] == "2026-03"
    assert region.field_path(data, expected) == march_path
    assert region.field_age_s(data, expected) is not None
    assert not region.legacy_field_path(data).exists()


def test_legacy_file_and_missing_releases_cannot_be_a_cache_hit(world, tmp_path, releases, monkeypatch):
    monkeypatch.delenv("AGROCAST_RELEASE_MANIFEST_FILE", raising=False)
    path = region.legacy_field_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"meta":{"start":"2026-10"}}')
    with pytest.raises(ValueError):
        region.region_identity("2026-10", world)
    identity = region.region_identity("2026-10", world, releases=releases)
    assert region.field_payload(tmp_path, identity) is None


def test_close_points_do_not_share_rounded_working_directory(world, tmp_path):
    from agrocast.serve.pipeline import point_config

    _, first = point_config(world, tmp_path / "data", 55.000001, 38.250001)
    _, second = point_config(world, tmp_path / "data", 55.000002, 38.250002)
    assert first != second
    assert Path(first).exists() and Path(second).exists()
    with pytest.raises(ValidationError):
        point_config(world, tmp_path, float("nan"), 38.25)


def test_releases_and_canonical_json_reject_undefined_values(tmp_path):
    with pytest.raises(ValueError):
        canonical_json({"nan": float("nan")})
    path = tmp_path / "release.json"
    path.write_text('{"data_release":"latest"}')
    with pytest.raises(ValidationError):
        Releases.from_file(path)
    with pytest.raises(ValidationError):
        CacheScope(namespace="owned")


def test_autopilot_uses_full_current_identity_not_recent_other_month(world, tmp_path, releases, monkeypatch):
    import pandas as pd
    from agrocast.autopilot.cycle import _refresh_region_fields
    from agrocast.core import timeutils

    data_root = tmp_path / "data"
    path = tmp_path / "release.json"
    path.write_text(releases.model_dump_json())
    monkeypatch.setenv("AGROCAST_RELEASE_MANIFEST_FILE", str(path))
    monkeypatch.setenv("AGROCAST_WORLD", str(world))
    monkeypatch.setenv("AGROCAST_DATA", str(data_root))
    monkeypatch.setattr(region, "REGIONS", {"krai": {}})
    monkeypatch.setattr(timeutils, "next_occurrence", lambda month: pd.Period("2026-03", "M"))
    cache = ResultCache(data_root)
    october = region.region_identity("2026-10", world, releases=releases)
    cache.write(october, {"meta": {"start": "2026-10"}})
    calls = []

    def build(start, world_dir, data_root, region="krai", workers=2, releases=None):
        from agrocast.serve.region import region_identity
        identity = region_identity(start, world_dir, region, releases)
        calls.append(identity.key())
        return cache.write(identity, {"meta": {"start": start}})

    monkeypatch.setattr(region, "build_field", build)
    registry = SimpleNamespace(log_event=lambda *args: None)
    assert _refresh_region_fields(registry) == ["krai"]
    assert _refresh_region_fields(registry) == []
    changed = Releases.model_validate({**releases.model_dump(), "data_release": "4" * 64})
    path.write_text(changed.model_dump_json())
    assert _refresh_region_fields(registry) == ["krai"]
    assert len(set(calls)) == 2
    assert cache.read(october).payload["meta"]["start"] == "2026-10"
