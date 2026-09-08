"""Read helpers over the warehouse.

Kept in one module so every analytics agent asks the same question the
same way. A metric defined twice is a metric that will disagree with
itself, and this is the seed of the semantic layer the chat interface
will compile against.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class Series:
    """One entity's tonnage over time, ordered oldest first."""

    grain: str
    entity_key: str
    entity_name: str
    direction: str
    measure: str
    periods: list[str]
    sort_keys: list[int]
    values: list[float]

    def __len__(self) -> int:
        return len(self.values)


_SERIES_SQL = text("""
    SELECT
        f.grain::text                                    AS grain,
        COALESCE(ap.iata_code, ap.airport_name, al.airline_name) AS entity_key,
        COALESCE(ap.airport_name, al.airline_name)       AS entity_name,
        f.direction::text                                AS direction,
        f.measure                                        AS measure,
        p.period_label                                   AS period_label,
        p.sort_key                                       AS sort_key,
        SUM(f.tonnage_kg)::float                         AS tonnage_kg
    FROM fact_cargo_movement f
    JOIN dim_period  p  ON p.period_id  = f.period_id
    LEFT JOIN dim_airport ap ON ap.airport_id = f.airport_id
    LEFT JOIN dim_airline al ON al.airline_id = f.airline_id
    WHERE (:grain IS NULL OR f.grain::text = :grain)
      -- Industry totals must never be mixed with individual carriers.
      AND (al.airline_id IS NULL OR al.is_aggregate = FALSE OR :include_aggregates)
    GROUP BY 1,2,3,4,5,6,7
    ORDER BY 1,2,4,5,7
""")


def load_series(
    session: Session,
    grain: str | None = None,
    include_aggregates: bool = False,
    min_points: int = 3,
) -> list[Series]:
    """Every entity's time series, long enough to be worth analysing."""
    rows = session.execute(
        _SERIES_SQL, {"grain": grain, "include_aggregates": include_aggregates}
    ).mappings().all()

    grouped: dict[tuple, Series] = {}
    for r in rows:
        key = (r["grain"], r["entity_key"], r["direction"], r["measure"])
        s = grouped.get(key)
        if s is None:
            s = Series(r["grain"], r["entity_key"], r["entity_name"],
                       r["direction"], r["measure"], [], [], [])
            grouped[key] = s
        s.periods.append(r["period_label"])
        s.sort_keys.append(r["sort_key"])
        s.values.append(float(r["tonnage_kg"]))

    return [s for s in grouped.values() if len(s) >= min_points]


def period_ids(session: Session) -> dict[str, int]:
    return {
        r[0]: r[1]
        for r in session.execute(text("SELECT period_label, period_id FROM dim_period"))
    }


def national_totals(session: Session) -> dict[tuple[str, str], float]:
    """Total tonnage per (period, direction), for share calculations."""
    rows = session.execute(text("""
        SELECT p.period_label, f.direction::text, SUM(f.tonnage_kg)::float
        FROM fact_cargo_movement f
        JOIN dim_period p ON p.period_id = f.period_id
        LEFT JOIN dim_airline al ON al.airline_id = f.airline_id
        WHERE f.grain = 'AIRPORT' AND (al.airline_id IS NULL OR al.is_aggregate = FALSE)
        GROUP BY 1,2
    """)).all()
    return {(r[0], r[1]): float(r[2]) for r in rows}
