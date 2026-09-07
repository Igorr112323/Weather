import math
from pathlib import Path

import numpy as np
import pandas as pd

ASOF_SCHEMA = "asof-v1"
EVAL_REPLAY_REVISED = "replay_revised"
EVAL_PROSPECTIVE = "prospective"


def months_of_delay(delay_days):
    d = int(delay_days)
    if d <= 0:
        return 0
    return int(math.ceil(d / 31.0))


def effective_cutoff(issue, delay_days):
    return issue - months_of_delay(delay_days)


def target_window_end(tgt, span):
    return tgt + (int(span) - 1)


def target_allowed(tgt, span, cutoff, embargo=0):
    return target_window_end(tgt, span) + int(embargo) <= cutoff


def vintages_available(config, issue):
    base = getattr(config, "vintages_dir", "") or ""
    if not base:
        return False
    marker = Path(base) / str(issue)
    return marker.is_dir() and any(marker.iterdir())


def evaluation_mode(config, issue):
    return EVAL_PROSPECTIVE if vintages_available(config, issue) else EVAL_REPLAY_REVISED


def release_context(config, issue, observation_cutoff=None, sources=None, evaluation=None):
    issue_p = pd.Period(str(issue), "M")
    cutoff = issue_p if observation_cutoff is None else min(issue_p, pd.Period(str(observation_cutoff), "M"))
    delay = int(getattr(config, "publication_delay_days", 0) or 0)
    return {
        "schema": ASOF_SCHEMA,
        "issue_month": str(issue_p),
        "observation_cutoff": str(cutoff),
        "publication_delay_days": delay,
        "train_cutoff": str(effective_cutoff(cutoff, delay)),
        "sources": {name: str(pd.Period(str(v), "M")) for name, v in (sources or {}).items()},
        "evaluation": evaluation or evaluation_mode(config, issue_p),
    }


def validate_context(context):
    if not isinstance(context, dict) or context.get("schema") != ASOF_SCHEMA:
        raise ValueError("as-of context requires the asof-v1 schema")
    issue = pd.Period(context["issue_month"], "M")
    cutoff = pd.Period(context["observation_cutoff"], "M")
    train = pd.Period(context["train_cutoff"], "M")
    if not (train <= cutoff <= issue):
        raise ValueError("as-of context requires train_cutoff <= observation_cutoff <= issue_month")
    if int(context.get("publication_delay_days", 0)) < 0:
        raise ValueError("publication_delay_days must be non-negative")
    if context.get("evaluation") not in (EVAL_REPLAY_REVISED, EVAL_PROSPECTIVE):
        raise ValueError("as-of evaluation mode must be replay_revised or prospective")
    for name, through in (context.get("sources") or {}).items():
        src = pd.Period(through, "M")
        if src > cutoff:
            raise ValueError(f"source {name} is marked available after the observation cutoff")
    return True


def target_end_periods(records, span):
    starts = pd.PeriodIndex([pd.Period(f"{int(y)}-{int(m):02d}", "M") for y, m in zip(records["year"], records["target_month"])])
    return starts + (int(span) - 1)


def walk_forward_masks(target_ends, fold_years, embargo=0):
    masks = {}
    for y in sorted(int(v) for v in set(fold_years)):
        fold_start = pd.Period(f"{y}-01", "M")
        limit = fold_start - 1 - int(embargo)
        masks[y] = np.asarray(target_ends <= limit, dtype=bool)
    return masks
