"""The metric registry: one definition per metric, and nowhere else.

A metric defined twice is a metric that will eventually disagree with
itself - the dashboard says one number, the chat answers another, and
neither is obviously wrong. Everything that serves a figure to a user
compiles against this registry.

It is also the security boundary. A generated query cannot name a
column, a table or a function; it can only name entries that appear
here, and each of those carries its own SQL. That is what makes "the
model never computes a number" enforceable rather than aspirational.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Grain(str, Enum):
    AIRPORT = "AIRPORT"
    AIRLINE = "AIRLINE"


@dataclass(frozen=True)
class Metric:
    """A measure, with the aggregate that computes it."""

    name: str
    description: str
    sql: str
    unit: str
    grains: frozenset[str] = field(default_factory=lambda: frozenset({"AIRPORT", "AIRLINE"}))

    def supports(self, grain: str) -> bool:
        return grain in self.grains


@dataclass(frozen=True)
class Dimension:
    """Something a metric can be grouped by."""

    name: str
    description: str
    sql: str
    grains: frozenset[str] = field(default_factory=lambda: frozenset({"AIRPORT", "AIRLINE"}))

    def supports(self, grain: str) -> bool:
        return grain in self.grains


@dataclass(frozen=True)
class Filter:
    """A predicate a caller may apply, with its bind parameter."""

    name: str
    description: str
    sql: str            # references :value
    value_type: str     # "str" | "int" | "float" | "bool"


# --------------------------------------------------------------- metrics --

METRICS: dict[str, Metric] = {
    m.name: m
    for m in [
        Metric(
            "tonnage_kg",
            "Total freight moved, in kilograms.",
            "SUM(tonnage_kg)",
            "kg",
        ),
        Metric(
            "tonnage_mt",
            "Total freight moved, in metric tonnes.",
            "SUM(tonnage_kg) / 1000.0",
            "MT",
        ),
        Metric(
            "prior_year_tonnage_mt",
            "The publisher's own prior-year figure, in metric tonnes.",
            "SUM(prior_year_tonnage_kg) / 1000.0",
            "MT",
        ),
        Metric(
            "growth_yoy_pct",
            "Year-on-year growth against the publisher's prior-year figure. "
            "Null when the prior year is zero, because a zero base has no "
            "meaningful percentage change.",
            "CASE WHEN SUM(prior_year_tonnage_kg) > 0 "
            "THEN (SUM(tonnage_kg) - SUM(prior_year_tonnage_kg)) "
            "     / SUM(prior_year_tonnage_kg) * 100.0 END",
            "%",
        ),
        Metric(
            "record_count",
            "Number of underlying fact rows.",
            "COUNT(*)",
            "rows",
        ),
        Metric(
            "source_count",
            "Number of distinct source documents behind the figure.",
            "COUNT(DISTINCT source_document_id)",
            "documents",
        ),
    ]
}


# ------------------------------------------------------------ dimensions --

DIMENSIONS: dict[str, Dimension] = {
    d.name: d
    for d in [
        Dimension("airport_iata", "Airport IATA code.", "airport_iata",
                  frozenset({"AIRPORT"})),
        Dimension("airport_name", "Airport name.", "airport_name",
                  frozenset({"AIRPORT"})),
        Dimension("airport_city", "City the airport serves.", "airport_city",
                  frozenset({"AIRPORT"})),
        Dimension("country", "Country.", "country"),
        Dimension("airline_name", "Airline.", "airline_name",
                  frozenset({"AIRLINE"})),
        Dimension("period", "Reporting period label.", "period"),
        Dimension("period_kind", "MONTH, FISCAL or ANNUAL.", "period_kind"),
        Dimension("calendar_year", "Calendar year.", "calendar_year"),
        Dimension("direction", "International, domestic or total.", "direction"),
        Dimension("publisher", "Publishing body.", "publisher"),
        Dimension("measure", "Which series the figure comes from.", "measure"),
    ]
}


# --------------------------------------------------------------- filters --

FILTERS: dict[str, Filter] = {
    f.name: f
    for f in [
        Filter("grain", "AIRPORT or AIRLINE.", "grain = :value", "str"),
        Filter("direction", "INTERNATIONAL, DOMESTIC or TOTAL.",
               "direction = :value", "str"),
        Filter("period", "An exact period label such as 2026-04.",
               "period = :value", "str"),
        Filter("period_from", "Periods at or after this label.",
               "sort_key >= (SELECT min(sort_key) FROM v_cargo_fact WHERE period = :value)",
               "str"),
        Filter(
            "period_kind",
            "MONTH, FISCAL or ANNUAL. Comparing a month against a year makes "
            "a ranking meaningless, so a league table should fix this.",
            "period_kind = :value", "str",
        ),
        Filter("calendar_year", "Calendar year.", "calendar_year = :value", "int"),
        Filter("country", "Country name.", "country = :value", "str"),
        Filter("airport_iata", "Airport IATA code.", "airport_iata = :value", "str"),
        Filter("airline_name", "Airline name.", "airline_name = :value", "str"),
        Filter("publisher", "Publishing body.", "publisher = :value", "str"),
        Filter(
            "exclude_aggregate_airlines",
            "Drop industry totals such as 'All Scheduled Indian Airlines'. "
            "Summing those beside individual carriers double-counts the market.",
            "(airline_is_aggregate IS NULL OR airline_is_aggregate = FALSE)",
            "bool",
        ),
        Filter("min_tonnage_kg", "Only rows above this tonnage.",
               "tonnage_kg >= :value", "float"),
    ]
}


# The only relations a compiled query may read.
ALLOWED_SOURCES = {
    "v_cargo_fact", "v_trend", "v_anomaly", "v_forecast", "v_source",
}


def describe() -> dict:
    """The registry as data, for API discovery and for prompting."""
    return {
        "metrics": {
            m.name: {"description": m.description, "unit": m.unit,
                     "grains": sorted(m.grains)}
            for m in METRICS.values()
        },
        "dimensions": {
            d.name: {"description": d.description, "grains": sorted(d.grains)}
            for d in DIMENSIONS.values()
        },
        "filters": {
            f.name: {"description": f.description, "type": f.value_type}
            for f in FILTERS.values()
        },
        "sources": sorted(ALLOWED_SOURCES),
    }
