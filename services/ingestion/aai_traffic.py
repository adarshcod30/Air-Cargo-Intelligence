"""Passengers and aircraft movements, per airport, from the AAI annexures.

The traffic release is published as several annexures from one page. This
project has only ever read Annexure IV, the freight tables, and treated the
rest as absent - an earlier probe for the others used a lowercase filename
and got a 404, which was read as "not published" rather than "wrong URL".

They are published, in the same layout:

    Annexure II   aircraft movements, per airport
    Annexure III  passengers, per airport

Both matter here because they are the denominators freight has been missing.
Tonnage per flight and tonnage per passenger are what separate an airport
whose cargo grew because it gained flights from one whose cargo grew because
each flight carried more - and the freight tables alone cannot tell them
apart.

Sub-annexures A, B and C are international, domestic and total, exactly as
in the freight release, so section detection keys off the annexure letter
rather than a word like "freight" that these documents do not contain.

    python -m services.ingestion.aai_traffic --limit 6
"""

from __future__ import annotations

import argparse
import io
import json
import re
from dataclasses import dataclass

from services.common.logging import get_logger
from services.common.models import Direction
from services.ingestion.fetcher import fetch
from services.ingestion.normalise import (
    AirportResolver,
    month_to_iso,
    parse_number,
    strip_non_latin,
)
from services.ingestion.parsers.aai_freight import (
    _MONTH_COMPACT_RX,
    _MONTH_RX,
    _NOISE,
    _STANDALONE_YEAR_RX,
)

log = get_logger(__name__)

BASE = "https://www.aai.aero/sites/default/files/traffic-news/"

# Which annexure carries which quantity. Annexure I is a category summary
# with no per-airport rows, so it is deliberately not listed.
ANNEXES = {
    "Annex2": ("aircraft_movements", "count"),
    "Annex3": ("pax_carried", "count"),
}

# A, B and C are international, domestic and total. Matching on the letter
# is necessary rather than stylistic: these documents never say "freight",
# which is the word the cargo parser's section markers look for.
_SUBSECTION = re.compile(r"ANNEXURE\s*-\s*I{1,4}\s*([ABC])\b", re.I)
_SECTION_BY_LETTER = {
    "A": Direction.INTERNATIONAL,
    "B": Direction.DOMESTIC,
    "C": Direction.TOTAL,
}


@dataclass
class TrafficRow:
    airport_raw: str
    airport_iata: str | None
    airport_name: str
    period: str
    direction: str
    metric: str
    value: float
    unit: str
    doc_key: str
    source_url: str


def _detect_period(text: str) -> str | None:
    """Reporting month from the header, same quirks as the freight release."""
    header = text[:1600]
    year = _STANDALONE_YEAR_RX.search(header)
    month = _MONTH_RX.search(header)
    if month is None:
        compact = re.sub(r"\s+", "", strip_non_latin(header))
        month = _MONTH_COMPACT_RX.search(compact)
    if not (month and year):
        return None
    try:
        return month_to_iso(month.group(1), int(year.group(1)))
    except ValueError:
        return None


def parse_traffic(payload: bytes, metric: str, unit: str,
                  doc_key: str, source_url: str,
                  resolver: AirportResolver) -> list[TrafficRow]:
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed")
        return []

    try:
        pdf = pdfplumber.open(io.BytesIO(payload))
    except Exception as exc:
        # An HTML error page saved with a .pdf name lands here, as it does
        # for the freight tables.
        log.warning(f"{doc_key}: not a readable PDF ({type(exc).__name__})")
        return []

    out: list[TrafficRow] = []
    section: Direction | None = None
    period: str | None = None

    with pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = _SUBSECTION.search(text)
            if m:
                section = _SECTION_BY_LETTER.get(m.group(1).upper(), section)
            # Continuation pages carry no header and inherit the section.
            if period is None:
                period = _detect_period(text)
            if section is None:
                continue

            for table in page.extract_tables():
                for row in table:
                    if not row or len(row) < 3:
                        continue
                    name_cell = (row[1] or "").strip() if len(row) > 1 else ""
                    airport = strip_non_latin(name_cell)
                    if not airport or _NOISE.match(airport):
                        continue
                    if not re.search(r"[A-Za-z]{3}", airport):
                        continue
                    value = parse_number(row[2] if len(row) > 2 else None)
                    if value is None or value < 0:
                        continue
                    record, confidence, _method = resolver.resolve(
                        airport, country_hint="India")
                    if confidence < 0.6:
                        continue
                    out.append(TrafficRow(
                        airport_raw=airport,
                        airport_iata=(record or {}).get("iata"),
                        airport_name=(record or {}).get("airport_name") or airport.title(),
                        period=period or "unknown",
                        direction=section.value,
                        metric=metric,
                        value=float(value),
                        unit=unit,
                        doc_key=doc_key,
                        source_url=source_url,
                    ))
    return out


def month_stems(limit: int | None = None) -> list[str]:
    """The month prefixes already proven to exist, from the freight documents.

    Reusing them rather than constructing a calendar means every fetch is for
    a release AAI has actually published.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from services.warehouse.loader import get_engine

    with Session(get_engine()) as s:
        urls = s.execute(text(
            "SELECT source_url FROM source_document WHERE publisher = 'AAI'"
        )).scalars().all()
    stems = sorted({re.sub(r"[Aa]nnex.*$", "", u.rsplit("/", 1)[-1]) for u in urls})
    stems = [s for s in stems if s]
    return stems[-limit:] if limit else stems


def ingest(limit: int | None = None) -> dict:
    resolver = AirportResolver()
    stems = month_stems(limit)
    collected: list[TrafficRow] = []
    fetched = failed = 0

    for stem in stems:
        for annex, (metric, unit) in ANNEXES.items():
            url = f"{BASE}{stem}{annex}.pdf"
            res = fetch(url)
            if not res.ok or not res.payload:
                failed += 1
                continue
            fetched += 1
            rows = parse_traffic(res.payload, metric, unit,
                                 doc_key=f"{stem}{annex}", source_url=url,
                                 resolver=resolver)
            collected.extend(rows)
            log.info(f"{stem}{annex}: {len(rows)} row(s)")

    return {
        "months": len(stems),
        "documents_fetched": fetched,
        "documents_missing": failed,
        "rows": len(collected),
        "_rows": collected,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest AAI passenger and movement annexures")
    ap.add_argument("--limit", type=int, default=None, help="most recent N months")
    args = ap.parse_args()

    from services.ingestion.aai_traffic_loader import load_traffic

    report = load_traffic(limit=args.limit)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
