import pandas as pd


def period(ts):
    return pd.Period(pd.Timestamp(ts), "M")


def now_period():
    return pd.Period(pd.Timestamp.now(), "M")


def shift(p, n):
    return p + n


def target_periods(start, horizon):
    return [start + i for i in range(horizon)]


def issue_period(start):
    return start - 1


def month_period(year, month):
    return pd.Period(f"{int(year)}-{int(month):02d}", "M")


def next_occurrence(month, base=None):
    b = base or now_period()
    year = b.year + (1 if month <= b.month else 0)
    return month_period(year, month)


def period_range_str(start, horizon):
    return [str(p) for p in target_periods(start, horizon)]
