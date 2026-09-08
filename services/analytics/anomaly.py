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


def detect_statistical(
    periods: list[str], values: list[float], threshold: float = 3.0, min_kg: float = 1000.0
) -> list[AnomalyPoint]:
    """Flag points far from the series' own robust centre.

    `min_kg` exists because percentage deviation is meaningless at tiny
    volumes: an airport moving 2kg one month and 20kg the next is a 900%
    rise and not worth an alert.
    """
    if len(values) < 6:
        return []
    z = robust_z_scores(values)
    median = float(np.median(values))
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
    periods: list[str], values: list[float], period: int = 12, threshold: float = 3.0
) -> list[AnomalyPoint]:
    """Flag points whose STL residual is extreme.

    This is what stops a December peak from being reported every year:
    the seasonal component is removed first, so only departures from the
    expected seasonal shape are flagged.
    """
    from services.analytics.trend import seasonal_decompose

    decomposed = seasonal_decompose(values, period=period)
    if decomposed is None:
        return []
    trend, seasonal, resid = decomposed
    z = robust_z_scores(list(resid))
    out: list[AnomalyPoint] = []
    for i, (p, v, zi) in enumerate(zip(periods, values, z, strict=True)):
        if abs(zi) < threshold:
            continue
        expected = float(trend[i] + seasonal[i])
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
    seas = {a.period: a for a in detect_seasonal(periods, values, threshold=threshold)}
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
