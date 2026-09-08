"""Parser for Open Government Data platform resources (data.gov.in).

Unlike AAI, there is no single layout to code against: the aviation
catalogue holds hundreds of datasets published by different bodies over
two decades, each with its own column names. So this parser does not
assume a schema - it *infers* one, scores its own confidence in that
inference, and reports which column it chose for what.

That inference is a structural decision, never a numeric one. Column
selection is auditable through `column_mapping` in the warnings; the
numbers themselves are read straight out of the chosen cells.
"""

from __future__ import annotations

import json
import re

from services.common.logging import get_logger
from services.common.models import (
    CargoFact,
    Direction,
    ExtractionResult,
    Grain,
    Publisher,
    SourceDocument,
)
from services.ingestion.normalise import (
    AirportResolver,
    month_to_iso,
    parse_number,
    to_kilograms,
)

log = get_logger(__name__)

# Column-name evidence, strongest first.
_AIRPORT_HINTS = ("airport", "aerodrome", "station", "airports")
_TONNAGE_HINTS = ("freight", "cargo", "tonnage", "tonnes", "tonne", "quantity", "weight")
_PERIOD_HINTS = ("year", "month", "period", "date", "quarter", "fy")
# Checked in order, and the combined patterns MUST come first. A title
# reading "Scheduled (International+Domestic) Services" contains the word
# "international", so testing single tokens first files a combined series
# as international-only - a wrong number, and one that then collides with
# the genuinely international series for the same carrier.
_COMBINED_RX = re.compile(
    r"international\s*[+&/]\s*domestic|domestic\s*[+&/]\s*international|"
    r"int'?l\s*[+&/]\s*dom|\btotal\b",
    re.I,
)
_DIRECTION_HINTS = {
    "international": Direction.INTERNATIONAL,
    "domestic": Direction.DOMESTIC,
    "total": Direction.TOTAL,
}

# Every cargo-bearing dataset in the aviation catalogue turned out to be
# airline-level, and the carrier is named only in the title:
#   "...Operating Statistics on Domestic Scheduled Services of Air Costa
#    from 2007-08 to 2015-16"
#   "Percentage Growth in Scheduled Cargo Traffic of Air India from ..."
# The greedy `.*` is deliberate: these titles contain several "of"s
# ("Details OF Annual Traffic ... Services OF Air Costa FROM 2007-08"),
# and it is the last one that introduces the carrier.
_AIRLINE_FROM_TITLE = re.compile(
    r".*\b(?:of|by)\s+(.+?)\s+(?:from|during|for|on|in)\b", re.I
)
# Names that denote the whole industry rather than one carrier.
_AGGREGATE_AIRLINE = re.compile(
    r"^(all\s|total\b|private carriers\b|scheduled (?:domestic|foreign) )", re.I
)

# Rate and ratio columns are not tonnage. Ingesting a percentage as a mass
# would be worse than ingesting nothing.
_NON_TONNAGE = re.compile(
    r"percent|growth|_kms|kilometer|kilometre|factor|per_|ratio|productivity|"
    # Aircraft specifications, not cargo actually moved. A fleet table's
    # "SIZE AV. PAYLOAD CAPACITY (TONNES)" matched on "tonnes" and was
    # being ingested as though the aircraft had carried that much.
    # Fleet tables prefix their specification columns with SIZE:
    #   "SIZE AV. PAYLOAD CAPACITY (TONNES)"
    #   "SIZE AV. M.C.T.OM WEIGHT (IN TONNE)"
    # The platform normalises punctuation to underscores, so match on the
    # separator-insensitive form rather than the printed one.
    r"capacity|payload|installed|seats|\bsize\b|m c t o|mctom|available",
    re.I,
)

_UNIT_IN_NAME = [
    (re.compile(r"\bkgs?\b|kilogram", re.I), "kg"),
    (re.compile(r"\bmt\b|metric[ _-]?ton|tonne|\btons?\b", re.I), "MT"),
]


def _pick(columns: list[str], hints: tuple[str, ...]) -> str | None:
    """Choose the column whose name best matches a hint."""
    lowered = {c: c.lower().replace("_", " ") for c in columns}
    for hint in hints:
        for col, low in lowered.items():
            if low.strip() == hint:
                return col
    for hint in hints:
        for col, low in lowered.items():
            if hint in low:
                return col
    return None


def infer_unit(column_name: str, default: str = "MT") -> str:
    """Read the unit out of a column name.

    Underscores are normalised to spaces first: in 'cargo_kg' the `_` is a
    word character, so `\bkg\b` never matches and the column would be
    read as metric tonnes - a silent 1000x error on every row.
    """
    normalised = re.sub(r"[_\-]+", " ", column_name)
    for rx, unit in _UNIT_IN_NAME:
        if rx.search(normalised):
            return unit
    return default


class DataGovInParser:
    """Schema-inferring parser for OGD resource payloads."""

    name = "data_gov_in_resource"

    def __init__(self, resolver: AirportResolver | None = None) -> None:
        self.resolver = resolver or AirportResolver()

    # ------------------------------------------------------------------ #

    def can_handle(self, doc: SourceDocument, payload: bytes) -> float:
        if payload[:1] not in (b"{", b"["):
            return 0.0
        try:
            data = json.loads(payload.decode("utf-8", "ignore"))
        except Exception:
            return 0.0
        if not isinstance(data, dict):
            return 0.0
        # The OGD envelope is distinctive: records plus a field schema.
        if "records" in data and ("field" in data or "index_name" in data):
            return 0.9
        return 0.0

    # ------------------------------------------------------------------ #

    def parse(self, doc: SourceDocument, payload: bytes) -> ExtractionResult:
        result = ExtractionResult(parser=self.name)
        try:
            data = json.loads(payload.decode("utf-8", "ignore"))
        except Exception as exc:
            result.warnings.append(f"invalid JSON: {exc}")
            return result

        records = data.get("records") or []
        if not records:
            result.warnings.append(f"no records; message={data.get('message', '')[:120]}")
            return result

        columns = list(records[0].keys())
        title = str(data.get("title") or doc.hints.get("title") or "")

        airport_col = _pick(columns, _AIRPORT_HINTS)
        tonnage_col = self._pick_tonnage(columns)
        period_col = _pick(columns, _PERIOD_HINTS)

        # No airport column means an airline-level dataset, which is what
        # this catalogue almost entirely contains. The carrier then has to
        # come from the title.
        airline = None if airport_col else _airline_from_title(title)
        grain = Grain.AIRPORT if airport_col else Grain.AIRLINE

        result.notes.append(
            "column_mapping="
            + json.dumps({
                "grain": grain.value, "airport": airport_col,
                "airline": airline, "tonnage": tonnage_col, "period": period_col,
            })
        )

        if not tonnage_col:
            result.warnings.append(f"no tonnage column among {columns[:12]}")
            return result
        if grain is Grain.AIRPORT and not airport_col:
            result.warnings.append("airport grain without an airport column")
            return result
        if grain is Grain.AIRLINE and not airline:
            # Refuse rather than attribute tonnage to an unknown carrier.
            result.warnings.append(f"cannot identify the airline from title: {title[:80]!r}")
            return result

        # One carrier can have several genuinely different series in the
        # same period and direction - "all international scheduled
        # services" is not "international traffic to and from India".
        # Without a discriminator the natural key treats them as one row
        # and the later load silently overwrites the earlier.
        measure = _measure_slug(title, tonnage_col)
        unit = infer_unit(tonnage_col)
        direction = self._infer_direction(data.get("title", ""), tonnage_col)
        default_period = self._infer_period(data.get("title", ""))

        carried_year: int | None = None
        for rec in records:
            result.rows_seen += 1
            # A row whose period cell names a year is a header for the rows
            # beneath it; remember the year and move on.
            header_year = _header_year(rec, period_col)
            if header_year is not None:
                carried_year = header_year
            tonnage = parse_number(rec.get(tonnage_col))
            if tonnage is None:
                continue
            period = (
                self._row_period(rec, period_col, carried_year)
                or default_period
                or "unknown"
            )

            if grain is Grain.AIRLINE:
                result.facts.append(
                    CargoFact(
                        airport_name_raw="",
                        period=period,
                        direction=direction,
                        tonnage_kg=to_kilograms(tonnage, unit),
                        source_document_id=doc.doc_id,
                        publisher=Publisher.DATA_GOV_IN,
                        country="India",
                        grain=Grain.AIRLINE,
                        airline=airline,
                        measure=measure,
                        is_aggregate=bool(_AGGREGATE_AIRLINE.match(airline or "")),
                        resolution_confidence=1.0,
                        resolution_method="airline-from-title",
                    )
                )
                result.rows_kept += 1
                continue

            raw_airport = str(rec.get(airport_col) or "").strip()
            if not raw_airport:
                continue
            record, confidence, method = self.resolver.resolve(
                raw_airport, country_hint="India"
            )
            result.facts.append(
                CargoFact(
                    airport_name_raw=raw_airport,
                    period=period,
                    direction=direction,
                    tonnage_kg=to_kilograms(tonnage, unit),
                    source_document_id=doc.doc_id,
                    publisher=Publisher.DATA_GOV_IN,
                    airport_iata=(record or {}).get("iata") or None,
                    airport_icao=(record or {}).get("icao") or None,
                    airport_name=(record or {}).get("airport_name") or raw_airport.title(),
                    country="India",
                    resolution_confidence=confidence,
                    resolution_method=method,
                )
            )
            result.rows_kept += 1

        from services.ingestion.parsers.base import score_extraction

        result.confidence = score_extraction(result)
        log.info(f"{self.name}: {result.summary()}")
        return result

    # ------------------------------------------------------------------ #

    @staticmethod
    def _pick_tonnage(columns: list[str]) -> str | None:
        """Choose a mass column, never a rate or ratio one.

        The catalogue is full of 'percentage growth in cargo traffic' and
        'tonne-kms performed' columns. Both contain a cargo hint word; one
        is a percentage and the other is a distance-weighted measure, and
        storing either as kilograms would be silently wrong.
        """
        # Separators are normalised first so "m_c_t_om" and "M.C.T.OM"
        # are rejected by the same pattern.
        def _spec(col: str) -> bool:
            return bool(_NON_TONNAGE.search(re.sub(r"[_.\-]+", " ", col)))

        safe = [c for c in columns if not _spec(c)]
        # A total is preferred over a component so freight and mail are
        # not double-counted when both are present.
        for preferred in ("total", "freight", "cargo"):
            for c in safe:
                if preferred in c.lower() and any(h in c.lower() for h in _TONNAGE_HINTS):
                    return c
        return _pick(safe, _TONNAGE_HINTS)

    @staticmethod
    def _infer_direction(title: str, column: str) -> Direction:
        blob = f"{title} {column}"
        if _COMBINED_RX.search(blob):
            return Direction.TOTAL
        lowered = blob.lower()
        for token, direction in _DIRECTION_HINTS.items():
            if token in lowered:
                return direction
        return Direction.TOTAL

    @staticmethod
    def _infer_period(title: str) -> str | None:
        m = re.search(r"\b(19|20)\d{2}\b", title or "")
        return f"{m.group(0)}-A" if m else None

    @staticmethod
    def _row_period(rec: dict, period_col: str | None,
                    carried_year: int | None = None) -> str | None:
        if not period_col:
            return None
        raw = str(rec.get(period_col) or "").strip()
        if not raw:
            return None

        # These tables put a fiscal-year header row above bare month names:
        #   2015-16 / APR / MAY / JUN ...
        # A month with no year of its own must inherit the header's, and
        # under an Indian fiscal year APR-DEC belong to the first calendar
        # year while JAN-MAR belong to the second.
        month = _MONTH_NAME.get(raw.strip().lower()[:3])
        if month is not None:
            if carried_year is None:
                return None
            year = carried_year if month >= 4 else carried_year + 1
            return f"{year}-{month:02d}"
        # '2007-08' is the Indian fiscal year 2007-08, NOT August 2007.
        # The tell is that the second part is the next year's last two
        # digits. Reading it as a month would file a full year of cargo
        # under one wrong month.
        m = re.match(r"^(\d{4})[-/](\d{1,2})$", raw)
        if m:
            year, tail = int(m.group(1)), int(m.group(2))
            if tail == (year + 1) % 100:
                return f"{year}-FY"
            if 1 <= tail <= 12:
                return f"{year}-{tail:02d}"
            return f"{year}-A"
        m = re.match(r"^([A-Za-z]+)[ \-,]+(\d{4})$", raw)
        if m:
            try:
                return month_to_iso(m.group(1), int(m.group(2)))
            except ValueError:
                pass
        m = re.search(r"\b((?:19|20)\d{2})\b", raw)
        if m:
            return f"{m.group(1)}-A"
        return None


# Words that only appear in a description, never inside a carrier's name.
_CLAUSE_WORDS = re.compile(
    r"\b(statistic|traffic|service|coefficient|aircraft|fleet|strength|"
    r"utilisation|utilization|growth|percentage|productivity|operating)\b",
    re.I,
)


def _airline_from_title(title: str) -> str | None:
    """Pull the carrier name out of a dataset title.

    Returns None when the pattern does not match, so the parser refuses
    rather than attributing tonnage to an unidentified airline.
    """
    m = _AIRLINE_FROM_TITLE.search(title or "")
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group(1)).strip(" ,.-")
    # Guard against the regex swallowing a clause instead of a name.
    # "Fleet Strength and Utilisation of Aircraft by Air India" previously
    # yielded "Aircraft by Air India"; matching "by" as well as "of" fixes
    # that, and these checks catch what still slips through.
    if not name:
        return None
    if len(name) > 48 or len(name.split()) > 5:
        return None
    if _CLAUSE_WORDS.search(name):
        return None
    return name


def _measure_slug(title: str, column: str) -> str:
    """A short, stable name for what a dataset measures.

    Built from the title with the carrier-agnostic parts kept and the
    years stripped, so the same series across two files slugs the same
    way while two different series stay distinct.
    """
    base = re.sub(r"\b(19|20)\d{2}(-\d{2,4})?\b", "", title or "")
    base = re.sub(r"\b(from|to|during|of|the|on|and|for|details|statistics)\b", " ", base, flags=re.I)
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if not base:
        base = re.sub(r"[^a-z0-9]+", "-", (column or "measure").lower()).strip("-")
    return base[:60] or "measure"


_MONTH_NAME = {
    m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], start=1)
}


def _header_year(rec: dict, period_col: str | None) -> int | None:
    """The calendar year a '2015-16' style header row establishes."""
    if not period_col:
        return None
    raw = str(rec.get(period_col) or "").strip()
    m = re.match(r"^((?:19|20)\d{2})\s*[-/]\s*\d{2,4}$", raw)
    if m:
        return int(m.group(1))
    if re.match(r"^(?:19|20)\d{2}$", raw):
        return int(raw)
    return None
