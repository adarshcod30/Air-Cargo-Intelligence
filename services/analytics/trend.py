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


# See seasonal_decompose: below this, STL interpolates its own
# seasonal component instead of estimating it.
MIN_STL_CYCLES = 5

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
    prior_year: list[float | None] | None = None,
) -> list[TrendPoint]:
    """Growth and share for one ordered series."""
    order = np.argsort(sort_keys)
    periods = [periods[i] for i in order]
    sort_keys = [sort_keys[i] for i in order]
    values = [values[i] for i in order]
    totals = [totals[i] for i in order] if totals else None
    prior_year = [prior_year[i] for i in order] if prior_year else None

    lag = _lag_for(periods)
    points: list[TrendPoint] = []
    for i, (p, sk, v) in enumerate(zip(periods, sort_keys, values, strict=True)):
        pt = TrendPoint(period=p, sort_key=sk, tonnage_kg=v)
        # Prefer the publisher's own prior-year figure when it gives one.
        # AAI exposes only a handful of recent months, so a derived
        # twelve-month lag would leave year-on-year growth permanently
        # null even though the number is printed on the page.
        if prior_year and prior_year[i]:
            pt.yoy_pct = pct_change(v, prior_year[i])
        elif i >= lag:
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

    Returns (trend, seasonal, residual) or None.

    Five complete cycles, not the two this used to require. Two was the
    textbook minimum for STL to run at all, which is not the same as the
    minimum for its residuals to mean anything.

    Each calendar month gets its own sub-series, one point per cycle, and
    LOESS through a handful of points interpolates rather than fits: the
    curve passes exactly through the first and last, so those get a
    residual of zero and the middle points carry the entire error. Measured
    over synthetic series with known seasonality, the share of sub-series
    endpoints landing within 1% of zero residual runs 67% at two cycles,
    99% at three, 37% at four, and 5% at five.

    Kolkata's domestic freight sat at three cycles. Its May seasonal
    component read +293, -818, -671, +2213 across four years, calling May
    below normal in the two years May was the annual peak, and the two
    middle Mays were reported as anomalies at +45% and +35%. Both were
    false positives produced by the decomposition rather than by the data.
    """
    if len(values) < MIN_STL_CYCLES * period:
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
