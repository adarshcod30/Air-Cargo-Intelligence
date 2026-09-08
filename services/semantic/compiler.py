"""Compiles a query specification into read-only SQL.

The caller never supplies SQL. It supplies *names* - of metrics, of
dimensions, of filters - and every one is looked up in the registry
before anything is emitted. A name that is not registered is rejected,
so there is no path from caller input to a SQL fragment.

Values are always bound as parameters, never interpolated. Together
those two rules mean a malicious or hallucinated request costs a 400,
not a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from services.semantic.registry import (
    ALLOWED_SOURCES,
    DIMENSIONS,
    FILTERS,
    METRICS,
)

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
MAX_LIMIT = 1000


class QueryRejected(ValueError):
    """A request that names something the registry does not define."""


@dataclass
class QuerySpec:
    source: str = "v_cargo_fact"
    metrics: list[str] = field(default_factory=lambda: ["tonnage_mt"])
    dimensions: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    order_by: str | None = None
    descending: bool = True
    limit: int = 50


@dataclass
class CompiledQuery:
    sql: str
    params: dict[str, Any]
    columns: list[str]
    spec: QuerySpec

    def explain(self) -> str:
        """A plain-language account of what was run, for the audit trail."""
        parts = [f"{', '.join(self.spec.metrics)} from {self.spec.source}"]
        if self.spec.dimensions:
            parts.append(f"by {', '.join(self.spec.dimensions)}")
        if self.spec.filters:
            parts.append(
                "where " + ", ".join(f"{k}={v!r}" for k, v in self.spec.filters.items())
            )
        if self.spec.order_by:
            parts.append(
                f"ordered by {self.spec.order_by} "
                f"{'desc' if self.spec.descending else 'asc'}"
            )
        return " ".join(parts) + f", limit {self.spec.limit}"


def compile_query(spec: QuerySpec) -> CompiledQuery:
    """Validate every name against the registry, then emit SQL."""
    if spec.source not in ALLOWED_SOURCES:
        raise QueryRejected(
            f"unknown source {spec.source!r}; allowed: {sorted(ALLOWED_SOURCES)}"
        )

    grain = str(spec.filters.get("grain") or "AIRPORT")

    if not spec.metrics:
        raise QueryRejected("at least one metric is required")

    select_parts: list[str] = []
    columns: list[str] = []

    for name in spec.dimensions:
        dim = DIMENSIONS.get(name)
        if dim is None:
            raise QueryRejected(f"unknown dimension {name!r}")
        if not dim.supports(grain):
            raise QueryRejected(
                f"dimension {name!r} does not apply at grain {grain}"
            )
        select_parts.append(f"{dim.sql} AS {name}")
        columns.append(name)

    for name in spec.metrics:
        metric = METRICS.get(name)
        if metric is None:
            raise QueryRejected(f"unknown metric {name!r}")
        if not metric.supports(grain):
            raise QueryRejected(f"metric {name!r} does not apply at grain {grain}")
        select_parts.append(f"{metric.sql} AS {name}")
        columns.append(name)

    where: list[str] = []
    params: dict[str, Any] = {}
    for i, (name, value) in enumerate(spec.filters.items()):
        flt = FILTERS.get(name)
        if flt is None:
            raise QueryRejected(f"unknown filter {name!r}")
        if flt.value_type == "bool":
            # A boolean filter is a switch, not a comparison: it either
            # applies or it does not.
            if value:
                where.append(flt.sql)
            continue
        key = f"p{i}"
        where.append(flt.sql.replace(":value", f":{key}"))
        params[key] = _coerce(flt.value_type, value, name)

    order = ""
    if spec.order_by:
        if spec.order_by not in columns:
            raise QueryRejected(
                f"cannot order by {spec.order_by!r}; it is not selected"
            )
        if not _IDENT.match(spec.order_by):
            raise QueryRejected("invalid order_by")
        order = f" ORDER BY {spec.order_by} {'DESC' if spec.descending else 'ASC'} NULLS LAST"

    limit = max(1, min(int(spec.limit), MAX_LIMIT))
    group = ""
    if spec.dimensions:
        group = " GROUP BY " + ", ".join(str(i + 1) for i in range(len(spec.dimensions)))

    sql = (
        f"SELECT {', '.join(select_parts)} FROM {spec.source}"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + group + order + f" LIMIT {limit}"
    )
    return CompiledQuery(sql=sql, params=params, columns=columns, spec=spec)


def _coerce(value_type: str, value: Any, name: str) -> Any:
    try:
        if value_type == "int":
            return int(value)
        if value_type == "float":
            return float(value)
        return str(value)
    except (TypeError, ValueError) as exc:
        raise QueryRejected(f"filter {name!r} expects {value_type}: {exc}") from exc
