"""Golden-fixture tests for the AAI freight parser.

`tests/fixtures/aai_annex4_sample.pdf` is page 1 of a real AAI Annexure-IV
release. Government publishers change PDF layouts without notice, and this
fixture is the tripwire: if AAI reshapes the table, these tests fail in CI
rather than the pipeline silently ingesting nothing.
"""

from pathlib import Path

import pytest

from services.common.models import Direction, Publisher, SourceDocument
from services.ingestion.parsers.aai_freight import AAIFreightParser

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aai_annex4_sample.pdf"


@pytest.fixture(scope="module")
def parsed():
    payload = FIXTURE.read_bytes()
    doc = SourceDocument(
        publisher=Publisher.AAI,
        source_url="https://www.aai.aero/sites/default/files/traffic-news/April2k26Annex4.pdf",
    )
    return AAIFreightParser().parse(doc, payload)


class TestAAIFreightParser:
    def test_fixture_exists(self):
        assert FIXTURE.exists(), "golden fixture missing"

    def test_extraction_clears_the_confidence_floor(self, parsed):
        assert parsed.confidence >= 0.60, parsed.summary()

    def test_yields_a_plausible_number_of_airports(self, parsed):
        # Page 1 is Annexure IV-A, roughly 60-70 international airports.
        assert 40 <= len(parsed.facts) <= 90, parsed.summary()

    def test_page_one_is_the_international_section(self, parsed):
        assert {f.direction for f in parsed.facts} == {Direction.INTERNATIONAL}

    def test_period_is_read_from_inside_the_document(self, parsed):
        """The month name and the year sit on different lines of the header,
        so a naive adjacent-token regex finds neither."""
        periods = {f.period for f in parsed.facts}
        assert periods == {"2026-04"}, periods

    def test_tonnage_is_stored_in_kilograms(self, parsed):
        """Chennai reads 29951.9 MT on the page; stored as kilograms."""
        chennai = next(f for f in parsed.facts if f.airport_iata == "MAA")
        assert chennai.tonnage_kg == pytest.approx(29_951_900.0, rel=1e-6)

    def test_major_hubs_are_present_and_resolved(self, parsed):
        codes = {f.airport_iata for f in parsed.facts}
        assert {"DEL", "BOM", "MAA", "BLR", "HYD"} <= codes

    def test_no_duplicate_airport_rows(self, parsed):
        """Duplicates silently double-count a city in every ranking."""
        keys = [(f.airport_iata or f.airport_name_raw, f.period, f.direction)
                for f in parsed.facts]
        assert len(keys) == len(set(keys))

    def test_structural_rows_are_not_treated_as_airports(self, parsed):
        """Category separators like 'INTERNATIONAL AIRPORTS' and the trailing
        'Note:' line look like data rows to a naive reader."""
        names = {f.airport_name_raw.upper() for f in parsed.facts}
        assert not any(n.startswith(("NOTE", "TOTAL", "S.NO")) for n in names)
        assert "INTERNATIONAL AIRPORTS" not in names

    def test_tonnage_is_never_negative(self, parsed):
        assert all(f.tonnage_kg >= 0 for f in parsed.facts)


class TestParserRefusesGarbage:
    def test_html_error_page_does_not_raise_and_yields_nothing(self):
        """AAI serves a 200-OK HTML error page at some .pdf URLs. The parser
        must report zero confidence rather than throwing or inventing rows."""
        doc = SourceDocument(publisher=Publisher.AAI, source_url="http://x/Annex4.pdf")
        result = AAIFreightParser().parse(doc, b"<html><body>Page not found</body></html>")
        assert result.facts == []
        assert result.confidence == 0.0
        assert result.warnings


class TestOlderReleaseLayouts:
    """A three-year backfill spans several header layouts.

    Reading the period wrong is not a cosmetic failure: every row in the
    file lands on period "unknown", whole sections then collapse onto one
    key, and the reconciler quarantines them as collisions. That is how
    a parsing bug shows up as a data-quality one.
    """

    @staticmethod
    def _detect(text):
        return AAIFreightParser._detect_period(text)

    def test_abbreviated_month_is_read(self):
        """Releases before ~2024 write JAN, not January."""
        header = ("SL. NO. AIRPORT JAN JAN % APR to JAN %\n"
                  "2023 2022 Change 2022-23 2021-22 Change")
        assert self._detect(header) == "2023-01"

    def test_full_month_still_read(self):
        header = ("Airport February February % April to February %\n"
                  "2026 2025 Change 2026-2027 2025-2026")
        assert self._detect(header) == "2026-02"

    def test_month_interleaved_with_devanagari(self):
        """Some releases interleave the Hindi and English text runs, so
        the header extracts as 'सि S त E बं P र' - the letters of SEP
        scattered between Devanagari glyphs."""
        header = ("SL. NO. AIRPORT सि S त E बं P र सि S त E बं P र % "
                  "अ A प्र P ैल R ि t े o सि S त E बं P र %\n2023 2022 Change")
        assert self._detect(header) == "2023-09"

    def test_month_without_a_word_boundary(self):
        """Once the gaps close the month sits inside a run like
        'AIRPORTSEPSEPAPRtoSEP', where \\b cannot anchor.

        Both months are interleaved in the real files, which is why the
        compact pass has to find the first one rather than the normal
        pass finding a conveniently spaced later one.
        """
        header = ("SL. NO. AIRPORT सि S त E बं P र सि S त E बं P र % "
                  "अ A प्र P ैल R ि t े o सि S त E बं P र %\n2024 2023 Change")
        assert self._detect(header) == "2024-09"

    def test_a_header_with_no_month_is_refused(self):
        """Guessing would misdate every row in the file."""
        assert self._detect("AIRPORT TOTAL 2023 2022 Change") is None

    def test_a_header_with_no_year_is_refused(self):
        assert self._detect("AIRPORT JAN JAN % Change") is None


class TestSectionHeadingVariants:
    """Section wording drifts across releases, and getting it wrong is
    not cosmetic: an undetected heading makes the section inherit the
    previous one, duplicating every airport in it. That surfaced as 1,590
    reconciliation collisions which were really a parsing failure."""

    @staticmethod
    def _match(line):
        from services.ingestion.parsers.aai_freight import _SECTION_MARKERS
        for pattern, direction in _SECTION_MARKERS:
            if pattern.search(line):
                return direction
        return None

    @pytest.mark.parametrize("heading,expected", [
        # Newer wording.
        ("Total Freight(Domestic+International)", "TOTAL"),
        ("घिेलू माल Domestic Freight", "DOMESTIC"),
        ("अंर्िातष्ट्रीय माल International Freight", "INTERNATIONAL"),
        # Older wording puts the qualifier BETWEEN the two words.
        ("( ) TOTAL(INTL DOM) FREIGHT", "TOTAL"),
        ("DOMESTIC FREIGHT", "DOMESTIC"),
        ("INTERNATIONAL FREIGHT", "INTERNATIONAL"),
    ])
    def test_headings_map_to_the_right_direction(self, heading, expected):
        d = self._match(heading)
        assert d is not None and d.value == expected

    def test_total_wins_over_its_component_words(self):
        """'TOTAL(INTL DOM) FREIGHT' contains INTL; a combined section
        must not be read as one of its parts."""
        assert self._match("( ) TOTAL(INTL DOM) FREIGHT").value == "TOTAL"

    def test_a_non_heading_line_matches_nothing(self):
        assert self._match("1 AMRITSAR 175.7 107.4 63.6%") is None
