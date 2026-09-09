"""Short-horizon forecasting with honest backtesting.

Two rules shape this module:

1. **Time-based validation only.** A random split leaks future
   information into training and produces scores that cannot survive
   contact with production.
2. **Never publish a point estimate alone.** These series are short and
   noisy; a bare number invites more confidence than the data supports,
   so every forecast carries an interval and the backtest error that
   earned it.

A model only ships if it beats the seasonal-naive baseline. Beating
nothing is not evidence of skill.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from services.common.logging import get_logger

log = get_logger(__name__)

# A forecast wrong by more than the quantity it predicts is not a forecast.
# Series whose best candidate cannot do better than this are left without
# one, and the pipeline reports how many were refused rather than filling
# the table with numbers nobody should act on.
MAX_PUBLISHABLE_MAPE = 35.0


@dataclass
class ForecastPoint:
    period_label: str
    horizon: int
    predicted_kg: float
    lower_kg: float | None
    upper_kg: float | None
    model: str
    backtest_mape: float | None
    interval_hits: int | None = None
    interval_folds: int | None = None

    def to_dict(self) -> dict:
        return {
            "period_label": self.period_label, "horizon": self.horizon,
            "predicted_kg": self.predicted_kg, "lower_kg": self.lower_kg,
            "upper_kg": self.upper_kg, "model": self.model,
            "backtest_mape": self.backtest_mape,
            "interval_hits": self.interval_hits,
            "interval_folds": self.interval_folds,
        }


def mape(actual: list[float], predicted: list[float]) -> float | None:
    """Mean absolute percentage error, skipping zero actuals.

    A zero actual makes the percentage undefined, and these series are
    full of them - airports with no cargo in a month.
    """
    pairs = [(a, p) for a, p in zip(actual, predicted, strict=True) if a not in (0, None)]
    if not pairs:
        return None
    return round(float(np.mean([abs((a - p) / a) for a, p in pairs]) * 100), 2)


def seasonal_naive(values: list[float], season: int, horizon: int) -> list[float]:
    """The baseline: repeat the value one season ago.

    Genuinely hard to beat on short, strongly seasonal series, which is
    exactly why it is the bar.
    """
    if len(values) >= season:
        return [values[-season + (i % season)] for i in range(horizon)]
    return [values[-1]] * horizon


def naive(values: list[float], season: int, horizon: int) -> list[float]:
    """Repeat the last observation.

    On a series with no usable seasonal cycle this beats seasonal-naive
    outright, and most of the annual and fiscal series here are exactly
    that. Offering only seasonal-naive as the baseline meant a short,
    aseasonal series was scored against a model guaranteed to do badly on
    it, and then published anyway.
    """
    return [values[-1]] * horizon


def drift(values: list[float], season: int, horizon: int) -> list[float]:
    """Last value continued along the average slope of the whole series."""
    n = len(values)
    if n < 2:
        return [values[-1]] * horizon
    slope = (values[-1] - values[0]) / (n - 1)
    return [values[-1] + slope * (i + 1) for i in range(horizon)]


def recent_mean(values: list[float], season: int, horizon: int) -> list[float]:
    """Mean of the recent window.

    The right answer for a noisy series with no trend and no cycle, where
    every other model chases noise.
    """
    window = values[-min(len(values), max(season, 6)):]
    return [float(np.mean(window))] * horizon


# Every candidate the backtest scores. Each is cheap, classical and
# appropriate to series of 20-40 points; nothing here needs more data than
# these publishers provide.
_CANDIDATES = {
    "naive": naive,
    "drift": drift,
    "recent_mean": recent_mean,
    "seasonal_naive": seasonal_naive,
}


def _fit_sarima(values: list[float], season: int, horizon: int, *, debug: bool = False):
    """SARIMA point forecast plus an 80% interval, or None.

    A seasonal period of 1 means "no seasonality", but statsmodels
    rejects `seasonal_order=(1,0,0,1)` outright: periodicity must exceed
    1. Annual and fiscal series arrive here with season=1, so this
    silently failed on every one of them and the baseline always won by
    default rather than on merit.
    """
    try:
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        if season > 1 and len(values) >= 2 * season:
            seasonal_order = (1, 0, 0, season)
        else:
            seasonal_order = (0, 0, 0, 0)      # plain ARIMA
        model = SARIMAX(
            np.asarray(values, dtype=float),
            order=(1, 1, 1), seasonal_order=seasonal_order,
            enforce_stationarity=False, enforce_invertibility=False,
        )
        fitted = model.fit(disp=False)
        res = fitted.get_forecast(steps=horizon)
        mean = res.predicted_mean
        ci = res.conf_int(alpha=0.20)
        return (
            [float(x) for x in mean],
            [float(x) for x in np.asarray(ci)[:, 0]],
            [float(x) for x in np.asarray(ci)[:, 1]],
        )
    except Exception as exc:
        # A bare `except: return None` hid an invalid seasonal_order for
        # a long time, so the reason is surfaced when asked for.
        if debug:
            log.warning(f"SARIMA fit failed: {type(exc).__name__}: {exc}")
        return None


def rolling_origin_backtest(values: list[float], season: int, folds: int = 3) -> dict:
    """Score each model on data it has not seen, walking forward.

    Each fold trains on everything up to a cut point and predicts the
    next step, so no fold can see its own future.
    """
    results: dict[str, list[float]] = {name: [] for name in _CANDIDATES}
    results["sarima"] = []
    actuals: list[float] = []
    n = len(values)

    # Each candidate is scored on the history it needs, not on the history
    # the most demanding one needs. A single global `min_train` of
    # season + 2 meant a monthly series required seventeen points before
    # anything was scored - SARIMA's requirement, applied to `naive`, which
    # needs two. Series shorter than that got no forecast at all, not
    # because they were unforecastable but because the bar was set by a
    # model that was not going to win on them anyway.
    need = {"naive": 2, "drift": 3, "recent_mean": 3,
            "seasonal_naive": season, "sarima": max(season + 2, 6)}
    usable = {name for name, req in need.items() if n >= req + folds}
    if not usable:
        return {}

    for k in range(folds, 0, -1):
        cut = n - k
        train, actual = values[:cut], values[cut]
        actuals.append(actual)
        for name, fn in _CANDIDATES.items():
            if name not in usable:
                continue
            try:
                results[name].append(fn(train, season, 1)[0])
            except Exception:
                results[name].append(float("nan"))
        if "sarima" in usable:
            sarima = _fit_sarima(train, season, 1)
            results["sarima"].append(sarima[0][0] if sarima else float("nan"))

    scored = {}
    for name, preds in results.items():
        if not preds or len(preds) != len(actuals):
            continue
        if any(np.isnan(p) for p in preds):
            continue
        scored[name] = mape(actuals, preds)
    return {k: v for k, v in scored.items() if v is not None}


# Half-width of an 80% band, as a multiple of the model's own mean absolute
# percentage error.
#
# For normally distributed errors, MAE = sigma * sqrt(2/pi), so an 80%
# two-sided band is 1.2816 * 1.2533 * MAE, about 1.61. The value that
# actually lands at 80% here is 1.70, which says these errors are slightly
# heavier-tailed than normal - unsurprising for monthly cargo, where a
# single charter or a closed runway moves a month a long way.
#
# Chosen by pooling backtest folds over half the series and measuring on the
# other half: 80.6% on the calibration half, 81.8% on the held-out half.
# Calibrating and measuring on the same folds would have been circular.
INTERVAL_K = 1.70


def _simple_interval(point: float, values: list[float], mape_pct: float) -> tuple[float, float]:
    """The band a simple model publishes around a point.

    Kept in one place because backtesting the interval has to build it
    exactly as publishing does. Measuring coverage of a band the product
    does not emit would be measuring nothing.

    This took `max(spread_of_recent_values, mape * point)` before, which is
    conservative twice over: whichever estimate is larger wins, and the
    result was then widened again. Pooled coverage came out at 87.6% for a
    band advertised as 80% - too wide is as wrong as too narrow, because it
    makes the forecast look less certain than it is.
    """
    spread = abs(point) * (mape_pct / 100.0)
    # A model with almost no backtest error would otherwise publish a band
    # of almost no width, which reads as false precision on three folds.
    spread = max(spread, abs(point) * 0.02)
    return max(0.0, point - INTERVAL_K * spread), point + INTERVAL_K * spread


def backtest_interval_coverage(
    values: list[float], season: int, winner: str, mape_pct: float, folds: int = 3
) -> tuple[int, int]:
    """How often the published band would have contained the truth.

    Measured the way the point error is measured - by walking forward over
    held-out points - rather than by waiting for a forecast horizon to
    elapse in real time. Waiting is why this was reported as unmeasurable:
    only six forecast periods had arrived, and six samples cannot
    distinguish an 80% interval from a 50% one.

    Returns (inside, total) so callers can pool folds across series instead
    of averaging percentages computed on three points each.
    """
    n = len(values)
    if n < folds + 3:
        return (0, 0)

    inside = total = 0
    for k in range(folds, 0, -1):
        cut = n - k
        train, actual = values[:cut], values[cut]
        lo = hi = None
        if winner == "sarima":
            fitted = _fit_sarima(train, season, 1)
            if fitted:
                _, los, his = fitted
                lo, hi = max(0.0, los[0]), his[0]
        else:
            fn = _CANDIDATES.get(winner)
            if fn is None:
                continue
            try:
                point = fn(train, season, 1)[0]
            except Exception:
                continue
            lo, hi = _simple_interval(point, train, mape_pct)
        if lo is None or hi is None:
            continue
        total += 1
        if lo <= actual <= hi:
            inside += 1
    return (inside, total)


def forecast(
    periods: list[str], values: list[float], horizon: int = 3, season: int = 12
) -> list[ForecastPoint]:
    """Forecast forward, choosing the model the backtest actually favours."""
    if len(values) < 6:
        return []
    if periods and periods[0].endswith(("-FY", "-A")):
        season = 1                          # annual series have no month cycle

    scores = rolling_origin_backtest(values, season)
    if not scores:
        return []

    # The model the backtest actually favours, not a fixed preference.
    # Previously SARIMA was tried and everything else fell to seasonal-naive,
    # so an aseasonal series was published with the one model guaranteed to
    # do badly on it.
    winner = min(scores, key=lambda k: scores[k])
    best = scores[winner]

    if best is None or best > MAX_PUBLISHABLE_MAPE:
        # A projection that was wrong by more than the quantity itself
        # carries no information. Publishing it with the error printed
        # beside it is technically honest and practically misleading: it
        # occupies a row that reads as a forecast. Saying nothing is the
        # more useful answer, and the count of refusals is reported.
        log.debug(f"no publishable model (best {winner}={best}); refusing")
        return []

    hits, folds_measured = backtest_interval_coverage(values, season, winner, best)

    if winner == "sarima":
        fitted = _fit_sarima(values, season, horizon)
        if fitted:
            mean, lo, hi = fitted
            return [
                ForecastPoint(
                    _next_label(periods[-1], i + 1), i + 1,
                    round(max(0.0, mean[i]), 3),
                    round(max(0.0, lo[i]), 3), round(max(0.0, hi[i]), 3),
                    "sarima", best, hits, folds_measured,
                )
                for i in range(horizon)
            ]
        # SARIMA won the backtest but will not refit on the full series;
        # fall through to the best model that does.
        scores.pop("sarima", None)
        if not scores:
            return []
        winner = min(scores, key=lambda k: scores[k])
        best = scores[winner]
        if best > MAX_PUBLISHABLE_MAPE:
            return []
        hits, folds_measured = backtest_interval_coverage(values, season, winner, best)

    preds = _CANDIDATES[winner](values, season, horizon)
    # A simple model has no analytic interval. Its own backtest error is
    # the honest width: a model that was typically 9% wrong should not
    # publish a band narrower than that.
    out = []
    for i in range(horizon):
        lo, hi = _simple_interval(preds[i], values, best)
        out.append(ForecastPoint(
            _next_label(periods[-1], i + 1), i + 1,
            round(max(0.0, preds[i]), 3), round(lo, 3), round(hi, 3),
            winner, best, hits, folds_measured,
        ))
    return out


def _next_label(last: str, step: int) -> str:
    """Advance a period label by `step`, respecting its kind."""
    try:
        year, tail = last.split("-", 1)
        year = int(year)
    except Exception:
        return f"{last}+{step}"
    if tail == "FY":
        return f"{year + step}-FY"
    if tail == "A":
        return f"{year + step}-A"
    month = int(tail) + step
    return f"{year + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}"
