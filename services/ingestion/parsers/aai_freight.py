"""Parser for AAI Annexure-IV, the monthly freight traffic report.

Structure observed in the April 2026 release (7 pages):

    IV-A  International Freight   page 1
    IV-B  Domestic Freight        pages 2-4
    IV-C  Total Freight           pages 5-7

Eight columns:
    S.No | Airport | month CY | month PY | % chg | FY-to-date CY | FY PY | % chg

Four quirks this parser exists to survive, all observed in real files:

1. Airport cells are bilingual in one cell: 'अमृतसर AMRITSAR'.
2. Section headings appear only on the FIRST page of a section, so pages
   2-4 carry no clue that they are Domestic. Section state is carried
   forward across pages.
3. Category separator rows ('INTERNATIONAL AIRPORTS') sit mid-table with
   an airport-shaped first cell and no numbers.
4. Values are metric tonnes and are converted to kilograms on the way in.
"""

from __future__ import annotations

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
    parse_percent,
    strip_non_latin,
    to_kilograms,
)

log = get_logger(__name__)

# Section headings are not worded consistently across releases. Newer
# files say "Total Freight(Domestic+International)"; older ones say
# "TOTAL(INTL DOM) FREIGHT", where the qualifier sits BETWEEN the two
# words. A rigid "total\s+freight" misses that, the TOTAL section then
# inherits DOMESTIC from the page before it, and every domestic airport
# is duplicated - 1,590 rows quarantined as collisions that were really
# a section-detection failure.
#
# TOTAL is tested first because its heading contains the component words.
_SECTION_MARKERS = [
    (re.compile(r"\btotal\b.{0,24}\bfreight\b|\bfreight\b.{0,24}\btotal\b", re.I),
     Direction.TOTAL),
    (re.compile(r"\bdomestic\b.{0,16}\bfreight\b", re.I), Direction.DOMESTIC),
    (re.compile(r"\binternational\b.{0,16}\bfreight\b", re.I), Direction.INTERNATIONAL),
]

# Rows that look like data but are structure.
_NOISE = re.compile(
    r"^(note|source|s\.?no|airport|total|grand total|all india|"
    r"international airports?|domestic airports?|customs airports?|"
    r"civil enclaves?|other airports?)\b",
    re.I,
)

# The month name and the year sit on DIFFERENT lines of the header block:
#   line 6:  'एयरपोरत Airport February February April To February'
#   line 9:  '2026 2025 2025-2026 2024-2025'
# So they are matched separately, and the year must be standalone rather
# than part of a fiscal range like '2025-2026'.
# Releases before ~2024 abbreviate the month - the header reads
#   JAN JAN % ... APR to JAN %
#   2023 2022 Change 2022-23 2021-22
# rather than spelling "January" out. Matching only full names left every
# row in those files on period "unknown", which then collapsed whole
# sections onto one key and quarantined them as collisions.
_MONTH_RX = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\b", re.I)
_STANDALONE_YEAR_RX = re.compile(r"(?<![\d-])(20\d{2})(?![\d-])")

# Once the Devanagari is stripped and the gaps closed, the month sits
# inside a run like "AIRPORTSEPSEPAPRtoSEP" with no word boundary to
# anchor on, so the compact pass matches without one.
_MONTH_COMPACT_RX = re.compile(
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)", re.I)


class AAIFreightParser:
    name = "aai_freight_annex4"

    def __init__(self, resolver: AirportResolver | None = None) -> None:
        self.resolver = resolver or AirportResolver()

    # ------------------------------------------------------------------ #

    def can_handle(self, doc: SourceDocument, payload: bytes) -> float:
        if not payload.startswith(b"%PDF-"):
            return 0.0
        score = 0.35
        url = doc.source_url.lower()
        if re.search(r"an+ex\s*4|an+ex4", url):      # tolerates the 'Anex' typo
            score += 0.4
        if "traffic-news" in url:
            score += 0.15
        return min(score, 0.95)

    # ------------------------------------------------------------------ #

    def parse(self, doc: SourceDocument, payload: bytes) -> ExtractionResult:
        result = ExtractionResult(parser=self.name)
        try:
            import pdfplumber
        except ImportError:
            result.warnings.append("pdfplumber not installed")
            return result

        import io

        try:
            pdf = pdfplumber.open(io.BytesIO(payload))
        except Exception as exc:
            # The AAI trap lands here: an HTML error page saved as .pdf.
            result.warnings.append(f"not a readable PDF: {type(exc).__name__}")
            return result

        section: Direction | None = None
        period: str | None = None

        with pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""

                for pattern, direction in _SECTION_MARKERS:
                    if pattern.search(text):
                        section = direction
                        break
                # Quirk 2: continuation pages inherit the previous section.

                if period is None:
                    period = self._detect_period(text)

                if section is None:
                    result.warnings.append(f"page {page_no}: no section context; skipped")
                    continue

                for table in page.extract_tables():
                    for row in table:
                        result.rows_seen += 1
                        fact = self._row_to_fact(row, section, period, doc)
                        if fact is not None:
                            result.facts.append(fact)
                            result.rows_kept += 1

        if period is None:
            result.warnings.append("reporting period not found in document text")
        if not result.facts:
            result.warnings.append("no data rows extracted")

        from services.ingestion.parsers.base import score_extraction

        result.confidence = score_extraction(result)
        log.info(f"{self.name}: {result.summary()}")
        return result

    # ------------------------------------------------------------------ #

    @staticmethod
    def _detect_period(text: str) -> str | None:
        """Read the reporting month from the header block.

        Takes the first month name and the first standalone year, which in
        this layout are the reporting month and its year respectively.
        """
        header = text[:1600]
        year = _STANDALONE_YEAR_RX.search(header)
        month = _MONTH_RX.search(header)

        if month is None:
            # Some releases interleave the Hindi and English runs, so the
            # header extracts as "सि S त E बं P र" - the letters of SEP
            # scattered between Devanagari glyphs. Dropping the Devanagari
            # and closing the gaps puts the month back together.
            compact = re.sub(r"\s+", "", strip_non_latin(header))
            month = _MONTH_COMPACT_RX.search(compact)

        if not (month and year):
            return None
        try:
            return month_to_iso(month.group(1), int(year.group(1)))
        except ValueError:
            return None

    def _row_to_fact(
        self, row: list, section: Direction, period: str | None, doc: SourceDocument
    ) -> CargoFact | None:
        if not row or len(row) < 4:
            return None

        name_cell = (row[1] or "").strip() if len(row) > 1 else ""
        airport = strip_non_latin(name_cell)
        if not airport or _NOISE.match(airport):
            return None                                    # quirk 3
        if not re.search(r"[A-Za-z]{3}", airport):
            return None

        current = parse_number(row[2] if len(row) > 2 else None)
        prior = parse_number(row[3] if len(row) > 3 else None)
        change = parse_percent(row[4] if len(row) > 4 else None)
        if current is None:
            return None

        record, confidence, method = self.resolver.resolve(airport, country_hint="India")

        return CargoFact(
            airport_name_raw=airport,
            period=period or "unknown",
            direction=section,
            tonnage_kg=to_kilograms(current, "MT"),        # quirk 4
            prior_year_tonnage_kg=(
                to_kilograms(prior, "MT") if prior is not None else None
            ),
            reported_change_pct=change,
            source_document_id=doc.doc_id,
            publisher=Publisher.AAI,
            airport_iata=(record or {}).get("iata") or None,
            airport_icao=(record or {}).get("icao") or None,
            airport_name=(record or {}).get("airport_name") or airport.title(),
            country="India",
            resolution_confidence=confidence,
            resolution_method=method,
        )
