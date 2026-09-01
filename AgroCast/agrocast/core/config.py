from dataclasses import dataclass, field, asdict
from pathlib import Path
import json


@dataclass
class Region:
    lat_min: float = 43.0
    lat_max: float = 47.0
    lon_min: float = 37.0
    lon_max: float = 42.0


@dataclass
class Config:
    data_dir: str = str(Path.home() / "agrocast_data")
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
    analog_k: int = 10
    n_pcs: int = 3
    horizon_max: int = 6
    n_ensemble: int = 200
    random_state: int = 0
    shared_zarr: str = ""

    def zarr_store(self):
        from agrocast.store.zarrstore import ZarrStore

        return ZarrStore(self.zarr_dir, self.shared_zarr or None)

    def dir(self, name):
        p = Path(self.data_dir) / name
        p.mkdir(parents=True, exist_ok=True)
        return p

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
        return Path(self.data_dir) / "registry.sqlite"

    def to_dict(self):
        d = asdict(self)
        return d

    def save(self, path=None):
        path = path or str(Path(self.data_dir) / "config.json")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path):
        d = json.loads(Path(path).read_text())
        d["region"] = Region(**d["region"])
        return cls(**d)
