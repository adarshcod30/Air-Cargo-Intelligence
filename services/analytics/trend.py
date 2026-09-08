"""Trend computation: growth, share, and seasonal decomposition.

Deterministic and testable in isolation. No model is involved - these are
the numbers the language model will later be handed to narrate, and the
whole grounding guarantee rests on them being computed here rather than
generated.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class TrendPoint:
    period: str
    sort_key: int
    tonnage_kg: float
    yoy_pct: float | None = None
    mom_pct: float | None = None
    cagr_pct: float | None = None
    share_of_total: float | None = None
    share_shift_pp: float | None = None


def pct_change(current: float, prior: float) -> float | None:
    """Percentage change, refusing to divide by a zero base.

    A zero prior is common in this data - an airport with no cargo last
    year - and 'infinite growth' is not a useful number to publish.
    """
    if prior is None or current is None or prior == 0:
        return None
    return round((current - prior) / prior * 100.0, 2)


def cagr(first: float, last: float, periods: int) -> float | None:
    """Compound growth. Undefined when the base is zero or the series is
    a single point, and negative values make the root meaningless."""
    if periods < 1 or first is None or last is None or first <= 0 or last < 0:
        return None
    return round(((last / first) ** (1.0 / periods) - 1.0) * 100.0, 2)


def _lag_for(periods: list[str]) -> int:
    """How many steps back a year is, for this period kind.

    Monthly series need a 12-step lag for year-on-year; annual and fiscal
    series need 1. Using 12 on an annual series silently produces no YoY
    at all, which looks like missing data rather than a bug.
    """
    if periods and periods[0].endswith(("-FY", "-A")):
        return 1
    return 12


def compute_trend(
    periods: list[str],
    sort_keys: list[int],
    values: list[float],
    totals: list[float] | None = None,
) -> list[TrendPoint]:
    """Growth and share for one ordered series."""
    order = np.argsort(sort_keys)
    periods = [periods[i] for i in order]
    sort_keys = [sort_keys[i] for i in order]
    values = [values[i] for i in order]
    totals = [totals[i] for i in order] if totals else None

    lag = _lag_for(periods)
    points: list[TrendPoint] = []
    for i, (p, sk, v) in enumerate(zip(periods, sort_keys, values, strict=True)):
        pt = TrendPoint(period=p, sort_key=sk, tonnage_kg=v)
        if i >= lag:
            pt.yoy_pct = pct_change(v, values[i - lag])
        if i >= 1:
            pt.mom_pct = pct_change(v, values[i - 1])
        if i >= 1:
            pt.cagr_pct = cagr(values[0], v, i)
        if totals and totals[i]:
            pt.share_of_total = round(v / totals[i] * 100.0, 3)
            if i >= lag and totals[i - lag]:
                prior_share = values[i - lag] / totals[i - lag] * 100.0
                pt.share_shift_pp = round(pt.share_of_total - prior_share, 3)
        points.append(pt)
    return points


def seasonal_decompose(values: list[float], period: int = 12):
    """STL decomposition, when the series is long enough to support one.

    Returns (trend, seasonal, residual) or None. STL needs at least two
    full cycles; forcing it on a shorter series produces components that
    look meaningful and are not.
    """
    if len(values) < 2 * period:
        return None
    try:
        from statsmodels.tsa.seasonal import STL

        arr = np.asarray(values, dtype=float)
        if np.allclose(arr, arr[0]):
            return None                      # a flat series has no seasonality
        res = STL(arr, period=period, robust=True).fit()
        return res.trend, res.seasonal, res.resid
    except Exception:
        return None
