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
_DIRECTION_HINTS = {
    "international": Direction.INTERNATIONAL,
    "domestic": Direction.DOMESTIC,
    "total": Direction.TOTAL,
}

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
        airport_col = _pick(columns, _AIRPORT_HINTS)
        tonnage_col = _pick(columns, _TONNAGE_HINTS)
        period_col = _pick(columns, _PERIOD_HINTS)

        result.warnings.append(
            "column_mapping="
            + json.dumps({"airport": airport_col, "tonnage": tonnage_col, "period": period_col})
        )

        if not (airport_col and tonnage_col):
            # Refuse rather than emit rows keyed on a guessed column.
            result.warnings.append(
                f"cannot map required columns from {columns[:12]}"
            )
            return result

        unit = infer_unit(tonnage_col)
        direction = self._infer_direction(data.get("title", ""), tonnage_col)
        default_period = self._infer_period(data.get("title", ""))

        for rec in records:
            result.rows_seen += 1
            raw_airport = str(rec.get(airport_col) or "").strip()
            tonnage = parse_number(rec.get(tonnage_col))
            if not raw_airport or tonnage is None:
                continue

            period = self._row_period(rec, period_col) or default_period or "unknown"
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
    def _infer_direction(title: str, column: str) -> Direction:
        blob = f"{title} {column}".lower()
        for token, direction in _DIRECTION_HINTS.items():
            if token in blob:
                return direction
        return Direction.TOTAL

    @staticmethod
    def _infer_period(title: str) -> str | None:
        m = re.search(r"\b(19|20)\d{2}\b", title or "")
        return f"{m.group(0)}-A" if m else None

    @staticmethod
    def _row_period(rec: dict, period_col: str | None) -> str | None:
        if not period_col:
            return None
        raw = str(rec.get(period_col) or "").strip()
        if not raw:
            return None
        m = re.match(r"^(\d{4})[-/](\d{1,2})$", raw)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}"
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
