import hashlib
import json

import numpy as np
import pandas as pd

FACT_MISSING = "fact_window_missing"
FACT_INCOMPLETE = "fact_incomplete"
FACT_NOT_FINITE = "fact_not_finite"


def month_completeness(daily, variables=("t2m", "tp")):
    if len(daily) == 0:
        return pd.DataFrame(columns=["days_expected", "days_present", "complete"])
    periods = pd.PeriodIndex(daily.index, freq="M")
    holder = daily.assign(_period=periods)
    rows = {}
    for period, block in holder.groupby("_period", sort=True):
        expected = int(period.days_in_month)
        present_days = set(block.index.normalize())
        entry = {"days_expected": expected, "days_present": len(present_days)}
        all_ok = entry["days_present"] == expected
        for v in variables:
            if v not in block:
                continue
            values = block[v].to_numpy(dtype="float64")
            finite_days = len({d for d, ok in zip(block.index.normalize(), np.isfinite(values)) if ok})
            entry[f"{v}_finite_days"] = int(finite_days)
            entry[f"{v}_ok"] = bool(finite_days == expected and entry["days_present"] == expected)
            all_ok = all_ok and entry[f"{v}_ok"]
        entry["complete"] = bool(all_ok)
        rows[period] = entry
    return pd.DataFrame.from_dict(rows, orient="index").sort_index()


def window_verdict(comp, start, end, obs_z, variables=("t2m", "tp")):
    if comp is None or len(comp) == 0:
        return False, FACT_MISSING
    span = pd.period_range(start, end, freq="M")
    if not span.isin(comp.index).all():
        return False, FACT_MISSING
    window = comp.loc[span]
    for v in variables:
        col = f"{v}_ok"
        if col in window and not bool(window[col].all()):
            return False, FACT_INCOMPLETE
    if not np.isfinite(obs_z):
        return False, FACT_NOT_FINITE
    return True, None


def obs_revision(daily, variables=("t2m", "tp")):
    seed = {"first": None, "last": None, "rows": int(len(daily)), "vars": {}}
    if len(daily):
        seed["first"] = str(pd.Timestamp(daily.index.min()).date())
        seed["last"] = str(pd.Timestamp(daily.index.max()).date())
    for v in variables:
        if v in daily:
            values = daily[v].to_numpy(dtype="float64")
            seed["vars"][v] = {
                "finite": int(np.isfinite(values).sum()),
                "sum": float(np.nansum(values)),
            }
    return hashlib.sha256(json.dumps(seed, sort_keys=True).encode("utf-8")).hexdigest()[:16]
