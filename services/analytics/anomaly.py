"""Anomaly detection over cargo series.

The metric that decides whether an alert feed is worth reading is the
false-positive rate, not recall. A feed that cries wolf is turned off,
and then real anomalies go unseen too. So detection is deliberately
conservative: two independent methods, a severity band, and explicit
suppression of the seasonal peaks that are not news.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class AnomalyPoint:
    period: str
    observed_kg: float
    expected_kg: float | None
    deviation_pct: float | None
    z_score: float | None
    method: str
    severity: str

    def to_dict(self) -> dict:
        return {
            "period": self.period, "observed_kg": self.observed_kg,
            "expected_kg": self.expected_kg, "deviation_pct": self.deviation_pct,
            "z_score": self.z_score, "method": self.method, "severity": self.severity,
        }


def _severity(z: float) -> str:
    a = abs(z)
    if a >= 4.0:
        return "HIGH"
    if a >= 3.0:
        return "MEDIUM"
    return "LOW"


def robust_z_scores(values: list[float]) -> np.ndarray:
    """Modified z-score using the median and MAD.

    The mean and standard deviation are themselves dragged by the outlier
    being looked for, which masks exactly the points that matter. The
    median and median absolute deviation are not, so a single large spike
    still stands out instead of inflating its own threshold.
    """
    arr = np.asarray(values, dtype=float)
    median = np.median(arr)
    mad = np.median(np.abs(arr - median))
    if mad == 0:
        std = arr.std()
        if std == 0:
            return np.zeros_like(arr)
        return (arr - arr.mean()) / std
    return 0.6745 * (arr - median) / mad


def trim_leading_zeros(
    periods: list[str], values: list[float]
) -> tuple[list[str], list[float]]:
    """Drop the run of zeros a series opens with.

    Those zeros are almost always "before this route existed" rather than
    "cargo collapsed to nothing", and leaving them in makes the launch
    itself register as the anomaly.
    """
    first = next((i for i, v in enumerate(values) if v > 0), len(values))
    return periods[first:], values[first:]


def detect_statistical(
    periods: list[str], values: list[float], threshold: float = 3.0,
    min_kg: float = 1000.0, min_baseline_kg: float = 10_000.0,
    min_nonzero_fraction: float = 0.5, min_median_to_max: float = 0.15,
) -> list[AnomalyPoint]:
    """Flag points far from the series' own robust centre.

    Three guards, each from a false positive this produced on real data:

    `min_kg` - percentage deviation is meaningless at tiny volumes. An
    airport moving 2kg one month and 20kg the next is a 900% rise and
    worth nobody's attention.

    `min_baseline_kg` and `min_nonzero_fraction` - a series that is mostly
    zeros has a median near zero, so *every* real value scores as a wild
    outlier. SpiceJet's international cargo was reported as "+29,886%
    versus an expected 14 tonnes" when what actually happened is that the
    airline started flying international routes. That is a structural
    break, not an anomaly, and reporting it as one is how an alert feed
    loses its reader.
    """
    periods, values = trim_leading_zeros(periods, values)
    if len(values) < 6:
        return []

    nonzero = [v for v in values if v > 0]
    if len(nonzero) / len(values) < min_nonzero_fraction:
        return []

    median = float(np.median(values))
    if median < min_baseline_kg:
        return []

    # A robust z-score assumes a roughly stationary series. When the
    # median is a tiny fraction of the maximum the series is not
    # stationary at all - it is a ramp or a regime change, and every
    # later point scores as an outlier against its own early history.
    #
    # SpiceJet's international cargo runs 0,0,0,0,0,0,9,14,1496,...,6506:
    # an airline starting international routes. Its median is 14 tonnes
    # against a maximum of 6,506, and reporting "+29,886% versus an
    # expected 14 tonnes" is how an alert feed loses its reader.
    peak = float(np.max(values))
    if peak > 0 and median / peak < min_median_to_max:
        return []

    z = robust_z_scores(values)
    out: list[AnomalyPoint] = []
    for p, v, zi in zip(periods, values, z, strict=True):
        if abs(zi) < threshold or v < min_kg:
            continue
        out.append(
            AnomalyPoint(
                period=p, observed_kg=float(v), expected_kg=median,
                deviation_pct=(round((v - median) / median * 100, 2) if median else None),
                z_score=round(float(zi), 3), method="robust_z",
                severity=_severity(float(zi)),
            )
        )
    return out


def detect_seasonal(
    periods: list[str], values: list[float], period: int = 12,
    threshold: float = 4.5, min_kg: float = 100_000.0,
    min_deviation_pct: float = 25.0,
) -> list[AnomalyPoint]:
    """Flag points whose STL residual is extreme.

    This is what stops a December peak from being reported every year:
    the seasonal component is removed first, so only departures from the
    expected seasonal shape are flagged.

    The threshold is higher than the raw-series one on purpose. Residuals
    are what is left after trend and season are removed, so their spread
    is far narrower and the same z-score means something much weaker.
    Reusing 3.0 here marked 1,670 points HIGH - more alerts than any feed
    could be read.

    The volume and deviation floors mirror the raw detector's, because a
    residual spike worth two percent of a small airport's month is not
    news whatever its z-score says.
    """
    from services.analytics.trend import seasonal_decompose

    periods, values = trim_leading_zeros(periods, values)
    decomposed = seasonal_decompose(values, period=period)
    if decomposed is None:
        return []
    trend, seasonal, resid = decomposed
    z = robust_z_scores(list(resid))
    out: list[AnomalyPoint] = []
    for i, (p, v, zi) in enumerate(zip(periods, values, z, strict=True)):
        if abs(zi) < threshold or v < min_kg:
            continue
        expected = float(trend[i] + seasonal[i])
        if expected <= 0:
            continue
        deviation = (v - expected) / expected * 100
        if abs(deviation) < min_deviation_pct:
            continue
        out.append(
            AnomalyPoint(
                period=p, observed_kg=float(v), expected_kg=expected,
                deviation_pct=(round((v - expected) / expected * 100, 2) if expected else None),
                z_score=round(float(zi), 3), method="stl_residual",
                severity=_severity(float(zi)),
            )
        )
    return out


def detect(periods: list[str], values: list[float], threshold: float = 3.0) -> list[AnomalyPoint]:
    """Both detectors, with agreement raising severity.

    A point both methods flag is far more likely to be real, so it is
    promoted rather than reported twice - which also keeps the feed short.
    """
    stat = {a.period: a for a in detect_statistical(periods, values, threshold)}
    # The seasonal detector keeps its own, stricter threshold: residual
    # z-scores are not on the same scale as raw ones.
    seas = {a.period: a for a in detect_seasonal(periods, values)}
    merged: list[AnomalyPoint] = []
    for p in sorted(set(stat) | set(seas)):
        if p in stat and p in seas:
            a = seas[p]                       # the seasonally aware estimate
            a.method = "consensus"
            a.severity = "HIGH" if a.severity != "LOW" else "MEDIUM"
            merged.append(a)
        else:
            merged.append(stat.get(p) or seas[p])
    return merged


# How much movement is worth a person's attention.
#
# 310 of 419 alerts sat on movements between 100 and 1,000 tonnes, at
# airports where Delhi alone handles 105,642. Those flags are statistically
# real and operationally noise: a 400% swing on 60 tonnes is a rounding
# artefact at national scale, and a feed full of them buries the movements
# that matter. The bar is absolute rather than relative because the question
# is whether a human should look, and that depends on tonnes, not sigma.
MATERIAL_MIN_MT = 500.0
MATERIAL_MIN_DEVIATION_PCT = 25.0


def is_material(point: AnomalyPoint) -> bool:
    """Is this movement large enough, in absolute terms, to act on?

    Applied after detection rather than inside it: the statistics stay
    honest about what is unusual, and this decides what is worth reporting.
    Keeping the two separate means the threshold can be argued about
    without touching the detector.
    """
    observed_mt = (point.observed_kg or 0.0) / 1000.0
    expected_mt = (point.expected_kg or 0.0) / 1000.0
    if max(observed_mt, expected_mt) < MATERIAL_MIN_MT:
        return False
    if abs(point.deviation_pct or 0.0) < MATERIAL_MIN_DEVIATION_PCT:
        return False
    return True
