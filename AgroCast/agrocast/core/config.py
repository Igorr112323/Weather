from dataclasses import dataclass, field, asdict
from pathlib import Path
import math

from agrocast.core.jsoncodec import strict_json
from agrocast.core.settings import DEFAULT_STATE
from agrocast.store.atomic import write_json


@dataclass
class Region:
    lat_min: float = 43.0
    lat_max: float = 47.0
    lon_min: float = 37.0
    lon_max: float = 42.0

    def __post_init__(self):
        if not all(type(value) in (int, float) and math.isfinite(value) for value in asdict(self).values()):
            raise ValueError("region bounds must be finite numbers")
        if not -90 <= self.lat_min < self.lat_max <= 90 or not -180 <= self.lon_min < self.lon_max <= 360:
            raise ValueError("region bounds are invalid")


@dataclass
class Config:
    data_dir: str = field(default_factory=lambda: str(DEFAULT_STATE / "compute"))
    region: Region = field(default_factory=Region)
    daily_grid: float = 0.5
    station_calibrated_targets: bool = True
    calib_end_year: int = 2004
    clim_window: int = 30
    clim_half_life: float = 20.0
    base_start: int = 1991
    base_end: int = 2020
    train_start: int = 1980
    backtest_start: int = 2000
    publication_delay_days: int = 5
    vintages_dir: str = ""
    analog_k: int = 10
    n_pcs: int = 3
    horizon_max: int = 6
    n_ensemble: int = 200
    random_state: int = 0
    shared_zarr: str = ""
    bundle_dir: str = ""
    runtime_dir: str = ""
    use_bundle_models: bool = False
    phys_preset: str = "land"
    regime_guard: bool = True
    ospr_enabled: bool = True
    ospr_weight: float = 0.2

    def __post_init__(self):
        self.validate()

    def validate(self):
        if not isinstance(self.region, Region):
            raise ValueError("region must be a Region")
        for name in ("calib_end_year", "clim_window", "base_start", "base_end", "train_start", "backtest_start", "analog_k", "n_pcs", "horizon_max", "n_ensemble", "random_state", "publication_delay_days"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        for name in ("station_calibrated_targets", "use_bundle_models", "regime_guard", "ospr_enabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if not 1 <= self.horizon_max <= 6 or min(self.clim_window, self.analog_k, self.n_pcs, self.n_ensemble) < 1 or self.random_state < 0 or self.publication_delay_days < 0:
            raise ValueError("numeric configuration is outside supported bounds")
        for name in ("daily_grid", "clim_half_life", "ospr_weight"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.daily_grid <= 0 or self.clim_half_life <= 0 or not 0 <= self.ospr_weight <= 1 or self.base_start > self.base_end:
            raise ValueError("numeric configuration is invalid")
        if self.phys_preset not in {"land", "state", "land_d6"}:
            raise ValueError("phys_preset is invalid")
        if self.bundle_dir:
            bundle, state = Path(self.bundle_dir).resolve(), Path(self.data_dir).resolve()
            if bundle == state or bundle in state.parents or state in bundle.parents:
                raise ValueError("bundle and computation state must not overlap")

    def zarr_store(self):
        from agrocast.store.zarrstore import ZarrStore

        if self.bundle_dir and self.runtime_dir and not self.use_bundle_models:
            return ZarrStore(self.zarr_dir, Path(self.runtime_dir) / "compute/zarr", self.shared_zarr or None)
        return ZarrStore(self.zarr_dir, self.shared_zarr or None)

    def dir(self, name):
        path = Path(self.data_dir) / name
        if path.resolve() != Path(self.data_dir).resolve() and Path(self.data_dir).resolve() not in path.resolve().parents:
            raise ValueError("write path escapes computation state")
        if self.bundle_dir and (Path(self.bundle_dir).resolve() == path.resolve() or Path(self.bundle_dir).resolve() in path.resolve().parents):
            raise ValueError("write path points into the bundle")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def raw_dir(self):
        return self.dir("raw")

    @property
    def zarr_dir(self):
        return self.dir("zarr")

    @property
    def artifact_dir(self):
        return self.dir("artifacts")

    @property
    def forecast_dir(self):
        return self.dir("forecasts")

    @property
    def registry_path(self):
        return self.dir(".") / "registry.sqlite"

    def artifact_path(self, name):
        local = Path(self.data_dir) / "artifacts" / name
        if local.exists() or not self.bundle_dir or not self.use_bundle_models:
            return local
        return Path(self.bundle_dir) / "artifacts" / name

    def source_artifact(self, name):
        local = Path(self.data_dir) / "artifacts" / name
        if local.exists() or not self.bundle_dir:
            return local
        if self.runtime_dir:
            common = Path(self.runtime_dir) / "compute/artifacts" / name
            if common.exists():
                return common
        return Path(self.bundle_dir) / "artifacts" / name

    def to_dict(self):
        return asdict(self)

    def save(self, path=None):
        target = Path(path) if path else Path(self.data_dir) / "config.json"
        if self.bundle_dir and (target.resolve() == Path(self.bundle_dir).resolve() or Path(self.bundle_dir).resolve() in target.resolve().parents):
            raise ValueError("cannot save configuration into the read-only bundle")
        return write_json(target, self.to_dict())

    @classmethod
    def load(cls, path):
        return cls.from_dict(strict_json(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, value):
        data = dict(value)
        data["region"] = Region(**data["region"])
        return cls(**data)
