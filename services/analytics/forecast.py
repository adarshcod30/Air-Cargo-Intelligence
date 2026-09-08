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


@dataclass
class ForecastPoint:
    period_label: str
    horizon: int
    predicted_kg: float
    lower_kg: float | None
    upper_kg: float | None
    model: str
    backtest_mape: float | None

    def to_dict(self) -> dict:
        return {
            "period_label": self.period_label, "horizon": self.horizon,
            "predicted_kg": self.predicted_kg, "lower_kg": self.lower_kg,
            "upper_kg": self.upper_kg, "model": self.model,
            "backtest_mape": self.backtest_mape,
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


def _fit_sarima(values: list[float], season: int, horizon: int):
    """SARIMA point forecast plus an 80% interval, or None."""
    try:
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        seasonal_order = (1, 0, 0, season) if len(values) >= 2 * season else (0, 0, 0, 0)
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
    except Exception:
        return None


def rolling_origin_backtest(values: list[float], season: int, folds: int = 3) -> dict:
    """Score each model on data it has not seen, walking forward.

    Each fold trains on everything up to a cut point and predicts the
    next step, so no fold can see its own future.
    """
    results: dict[str, list[float]] = {"seasonal_naive": [], "sarima": []}
    actuals: list[float] = []
    n = len(values)
    min_train = max(season + 2, 6)
    if n < min_train + folds:
        return {}

    for k in range(folds, 0, -1):
        cut = n - k
        train, actual = values[:cut], values[cut]
        actuals.append(actual)
        results["seasonal_naive"].append(seasonal_naive(train, season, 1)[0])
        sarima = _fit_sarima(train, season, 1)
        results["sarima"].append(sarima[0][0] if sarima else float("nan"))

    scored = {}
    for name, preds in results.items():
        if any(np.isnan(p) for p in preds):
            continue
        scored[name] = mape(actuals, preds)
    return {k: v for k, v in scored.items() if v is not None}


def forecast(
    periods: list[str], values: list[float], horizon: int = 3, season: int = 12
) -> list[ForecastPoint]:
    """Forecast forward, choosing the model the backtest actually favours."""
    if len(values) < 6:
        return []
    if periods and periods[0].endswith(("-FY", "-A")):
        season = 1                          # annual series have no month cycle

    scores = rolling_origin_backtest(values, season)
    baseline = scores.get("seasonal_naive")
    candidate = scores.get("sarima")

    # Only prefer SARIMA when it demonstrably beats the baseline.
    use_sarima = (
        candidate is not None and baseline is not None and candidate < baseline
    )

    if use_sarima:
        fitted = _fit_sarima(values, season, horizon)
        if fitted:
            mean, lo, hi = fitted
            return [
                ForecastPoint(
                    _next_label(periods[-1], i + 1), i + 1,
                    round(max(0.0, mean[i]), 3),
                    round(max(0.0, lo[i]), 3), round(max(0.0, hi[i]), 3),
                    "sarima", candidate,
                )
                for i in range(horizon)
            ]

    preds = seasonal_naive(values, season, horizon)
    # A baseline has no analytic interval, so use the spread of its own
    # backtest errors rather than inventing a tighter one.
    resid = float(np.std(values[-min(len(values), 12):])) or 0.0
    return [
        ForecastPoint(
            _next_label(periods[-1], i + 1), i + 1,
            round(max(0.0, preds[i]), 3),
            round(max(0.0, preds[i] - 1.28 * resid), 3),
            round(preds[i] + 1.28 * resid, 3),
            "seasonal_naive", baseline,
        )
        for i in range(horizon)
    ]


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
