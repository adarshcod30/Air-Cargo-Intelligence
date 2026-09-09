"""Structural measures over the cargo market.

These answer questions a single time series cannot:

  concentration    is the market consolidating onto a few airports?
  attribution      which airports actually moved the national number?
  belly dependency does a carrier's freight track its passenger flying?

Attribution is the honest analogue of "what is driving growth". The published
statistics carry no commodity breakdown, so the question cannot be answered
in terms of goods; it can be answered exactly in terms of *where*. Each
airport's contribution is expressed in percentage points of the national
change, and the parts sum to the whole - which is what makes it an
attribution rather than a ranking.

Nothing here is estimated. Every figure is arithmetic over stored rows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Contribution:
    entity_key: str
    entity_name: str
    now_mt: float
    then_mt: float
    change_mt: float
    contribution_pp: float          # percentage points of national growth
    share_of_change_pct: float      # share of the total movement

    def to_dict(self) -> dict:
        return {
            "entity_key": self.entity_key,
            "entity_name": self.entity_name,
            "now_mt": round(self.now_mt, 1),
            "then_mt": round(self.then_mt, 1),
            "change_mt": round(self.change_mt, 1),
            "contribution_pp": round(self.contribution_pp, 2),
            "share_of_change_pct": round(self.share_of_change_pct, 1),
        }


@dataclass
class Attribution:
    period_now: str
    period_then: str
    national_now_mt: float
    national_then_mt: float
    national_growth_pct: float
    contributors: list[Contribution] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "period_now": self.period_now,
            "period_then": self.period_then,
            "national_now_mt": round(self.national_now_mt, 1),
            "national_then_mt": round(self.national_then_mt, 1),
            "national_growth_pct": round(self.national_growth_pct, 2),
            "contributors": [c.to_dict() for c in self.contributors],
        }


def attribute_growth(
    now: dict[str, float], then: dict[str, float],
    names: dict[str, str] | None = None, top: int = 12,
) -> Attribution | None:
    """Decompose national change into per-airport contributions.

    Contribution is each airport's absolute change over the *national base*,
    not over its own base. An airport that doubled from 10 to 20 tonnes has
    a spectacular growth rate and a negligible contribution; expressing both
    in points of the national number is what stops a rounding error at a
    small airport from reading as a national trend.
    """
    names = names or {}
    base = sum(then.values())
    total_now = sum(now.values())
    if base <= 0:
        return None

    rows: list[Contribution] = []
    for key in set(now) | set(then):
        a, b = now.get(key, 0.0), then.get(key, 0.0)
        change = a - b
        if change == 0:
            continue
        rows.append(Contribution(
            entity_key=key,
            entity_name=names.get(key, key),
            now_mt=a, then_mt=b, change_mt=change,
            contribution_pp=100.0 * change / base,
            share_of_change_pct=0.0,
        ))

    gross = sum(abs(r.change_mt) for r in rows) or 1.0
    for r in rows:
        r.share_of_change_pct = 100.0 * abs(r.change_mt) / gross

    rows.sort(key=lambda r: abs(r.change_mt), reverse=True)
    return Attribution(
        period_now="", period_then="",
        national_now_mt=total_now, national_then_mt=base,
        national_growth_pct=100.0 * (total_now - base) / base,
        contributors=rows[:top],
    )


def herfindahl(values: list[float]) -> dict:
    """Market concentration, on the 0-10,000 scale competition authorities use.

    Reported alongside the effective number of competitors (10,000/HHI),
    because "HHI 1,850" means nothing to most readers while "equivalent to
    about 5 equally sized airports" does.
    """
    total = sum(v for v in values if v > 0)
    if total <= 0:
        return {"hhi": None, "effective_n": None, "interpretation": "no traffic"}
    shares = [100.0 * v / total for v in values if v > 0]
    hhi = sum(s * s for s in shares)
    eff = 10000.0 / hhi if hhi else None
    if hhi < 1500:
        note = "unconcentrated"
    elif hhi < 2500:
        note = "moderately concentrated"
    else:
        note = "highly concentrated"
    return {
        "hhi": round(hhi),
        "effective_n": round(eff, 1) if eff else None,
        "interpretation": note,
        "top_share_pct": round(max(shares), 1),
    }


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation, or None when it would not mean anything.

    Refuses fewer than four pairs and a constant series: both produce a
    number that looks like evidence and is not.
    """
    pairs = [(x, y) for x, y in zip(xs, ys)
             if x is not None and y is not None
             and math.isfinite(x) and math.isfinite(y)]
    n = len(pairs)
    if n < 4:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def describe_correlation(r: float | None) -> str:
    if r is None:
        return "too few comparable months to say"
    a = abs(r)
    strength = ("almost no" if a < 0.2 else "a weak" if a < 0.4
                else "a moderate" if a < 0.6 else "a strong" if a < 0.8 else "a very strong")
    direction = "positive" if r > 0 else "negative"
    return f"{strength} {direction} relationship (r = {r:.2f})"
