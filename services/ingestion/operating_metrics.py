"""Mining the operating statistics the cargo extractor discarded.

The cargo parser picks one tonnage column per dataset and drops the rest.
That is correct for a fact table whose every row is a mass of freight - a
percentage and a tonne-kilometre do not belong in a column called
`tonnage_kg`. But 257 other fields were being thrown away, and among them
are the two that make air-cargo efficiency measurable at all:

    FTK  freight tonne-kilometres performed - output actually delivered
    ATK  available tonne-kilometres         - capacity that was offered

Cargo load factor is FTK over ATK. It is the number an airport cargo team
manages against, it decides whether growth came from more capacity or from
using existing capacity better, and both sides of it were already on disk.

Nothing here re-downloads anything. The archived payloads are re-read, so
this is a parsing change rather than an ingestion run.

    python -m services.ingestion.operating_metrics --rebuild
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

from services.common.logging import get_logger
from services.common.models import Direction, Grain
from services.ingestion.parsers.datagovin import (
    _airline_from_title,
    _header_year,
    _pick,
    parse_number,
)

log = get_logger(__name__)

_PERIOD_HINTS = ("year_month", "month", "period", "_year", "year")


@dataclass(frozen=True)
class MetricSpec:
    """One canonical quantity and how it is written in the source."""

    metric: str
    unit: str
    patterns: tuple[str, ...]


# Canonical names, because the same quantity appears under several headings
# across the catalogue. Order matters: the first spec whose pattern matches a
# column claims it, so narrower patterns are listed before broader ones -
# `ton_kms_performed_million___freight` must not be claimed by the rule that
# matches `cargo_carried_ton___freight`.
_SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("ftk_million", "million tonne-km",
               (r"ton_?_?kms?_performed.*freight",)),
    MetricSpec("mail_tk_million", "million tonne-km",
               (r"ton_?_?kms?_performed.*mail",)),
    MetricSpec("ttk_million", "million tonne-km",
               (r"ton_?_?kms?_performed.*total",)),
    MetricSpec("pax_tk_million", "million tonne-km",
               (r"ton_?_?kms?_performed.*pax",)),
    MetricSpec("atk_million", "million tonne-km",
               (r"available_tonne_kilomet",)),
    MetricSpec("ask_million", "million seat-km",
               (r"available_seat_kilomet",)),
    MetricSpec("rpk_million", "million pax-km",
               (r"passengers?_+kms?_performed",)),
    MetricSpec("weight_load_factor", "percent",
               (r"^weight_load_factor",)),
    MetricSpec("pax_load_factor", "percent",
               (r"^pax_load_factor",)),
    MetricSpec("freight_tonnes", "tonnes",
               (r"cargo_carried_ton_+freight",)),
    MetricSpec("mail_tonnes", "tonnes",
               (r"cargo_carried_ton_+mail",)),
    MetricSpec("cargo_total_tonnes", "tonnes",
               (r"cargo_carried_ton_+total",)),
    MetricSpec("departures", "count",
               (r"aircraft_flown_+departures",)),
    MetricSpec("block_hours", "hours",
               (r"aircraft_flown_+hours",)),
    MetricSpec("distance_thousand_km", "thousand km",
               (r"aircraft_flown_+kms_thousand",)),
    MetricSpec("pax_carried", "count",
               (r"passengers?_+carried",)),
    MetricSpec("freight_inbound_tonnes", "tonnes",
               (r"freight_tonne_+to_india",)),
    MetricSpec("freight_outbound_tonnes", "tonnes",
               (r"freight_tonne_+from_india",)),
)

_COMPILED = [(s, tuple(re.compile(p, re.I) for p in s.patterns)) for s in _SPECS]


def classify(column: str) -> MetricSpec | None:
    """Which canonical quantity, if any, this column carries."""
    col = column.strip().lower()
    for spec, regexes in _COMPILED:
        if any(rx.search(col) for rx in regexes):
            return spec
    return None


def _direction(title: str, column: str) -> Direction:
    blob = f"{title} {column}".lower()
    if "international" in blob and "domestic" not in blob:
        return Direction.INTERNATIONAL
    if "domestic" in blob and "international" not in blob:
        return Direction.DOMESTIC
    return Direction.TOTAL


@dataclass
class OperatingMetric:
    grain: str
    entity_key: str
    period: str
    direction: str
    metric: str
    value: float
    unit: str
    source_doc_key: str


def extract_file(path: Path, doc_key: str) -> list[OperatingMetric]:
    """Every recognised quantity in one archived payload."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    records = data.get("records") or []
    if not records or not isinstance(records[0], dict):
        return []

    title = str(data.get("title") or "")
    airline = _airline_from_title(title)
    if not airline:
        # Airport-grain operating statistics are not published in this
        # catalogue; refusing is better than attributing a carrier's
        # load factor to an unknown entity.
        return []

    columns = list(records[0].keys())
    period_col = _pick(columns, _PERIOD_HINTS)
    wanted = [(c, classify(c)) for c in columns]
    wanted = [(c, s) for c, s in wanted if s is not None]
    if not wanted:
        return []

    out: list[OperatingMetric] = []
    carried_year: int | None = None
    for rec in records:
        header_year = _header_year(rec, period_col)
        if header_year is not None:
            carried_year = header_year
        period = _row_period(rec, period_col, carried_year)
        if not period:
            continue
        for col, spec in wanted:
            value = parse_number(rec.get(col))
            if value is None or value < 0:
                continue
            out.append(OperatingMetric(
                grain=Grain.AIRLINE.value,
                entity_key=airline[:64],
                period=period,
                direction=_direction(title, col).value,
                metric=spec.metric,
                value=float(value),
                unit=spec.unit,
                source_doc_key=doc_key,
            ))
    return out


_MONTH_RX = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]


def _row_period(rec: dict, period_col: str | None, carried_year: int | None) -> str | None:
    """The period this row belongs to, as an ISO month or a fiscal-year label."""
    raw = str(rec.get(period_col) or "").strip() if period_col else ""
    if not raw:
        return f"{carried_year}-FY" if carried_year else None

    iso = re.match(r"^(\d{4})-(\d{2})$", raw)
    if iso and 1 <= int(iso.group(2)) <= 12:
        return raw

    fy = re.match(r"^(\d{4})\s*[-/]\s*(\d{2,4})$", raw)
    if fy:
        return f"{fy.group(1)}-FY"

    m = _MONTH_RX.search(raw)
    if m and carried_year:
        return f"{carried_year}-{_MONTHS.index(m.group(1).lower()) + 1:02d}"

    if re.fullmatch(r"(19|20)\d{2}", raw):
        return f"{raw}-FY"
    return f"{carried_year}-FY" if carried_year else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract operating metrics from the archive")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    from services.ingestion.operating_loader import load_operating_metrics

    print(json.dumps(load_operating_metrics(rebuild=args.rebuild, limit=args.limit), indent=2))


if __name__ == "__main__":
    main()
