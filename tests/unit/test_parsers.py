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
