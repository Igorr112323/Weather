def nn_alpha_allowed(variable, config=None):
    if variable != "tp":
        return True
    return bool(getattr(config, "allow_tp_nn_alpha", False))
