from agrocast.models.clim import ClimModel
from agrocast.models.window import WindowedRidge, LAND_PATTERN, OCEAN_PATTERN, STRAT_PATTERN
from agrocast.models.analog import AnalogModel
from agrocast.models.ridge import RidgeModel
from agrocast.models.gbm import GBMModel
from agrocast.models.deep_analog import DeepAnalogModel
from agrocast.models.ssw import SSWModel

MODEL_NAMES = [
    "clim",
    "analog",
    "ridge",
    "gbm",
    "ridge_land",
    "ridge_ocean",
    "ridge_strat",
    "deep_analog",
    "ssw",
]


def build_models(config):
    return [
        ClimModel(),
        AnalogModel(k=config.analog_k),
        RidgeModel(),
        GBMModel(seed=config.random_state),
        WindowedRidge("ridge_land", LAND_PATTERN),
        WindowedRidge("ridge_ocean", OCEAN_PATTERN),
        WindowedRidge("ridge_strat", STRAT_PATTERN),
        DeepAnalogModel(k=getattr(config, "deep_analog_k", 12), n_pcs=getattr(config, "deep_analog_pcs", 5)),
        SSWModel(),
    ]
