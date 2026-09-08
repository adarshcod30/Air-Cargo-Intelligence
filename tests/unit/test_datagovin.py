"""Tests for the data.gov.in catalogue client and schema-inferring parser."""

import json

import pytest

from services.common.models import Direction, Publisher, SourceDocument
from services.ingestion.datagovin_catalog import OgdResource
from services.ingestion.parsers.datagovin import DataGovInParser, _pick, infer_unit


class TestUnitInference:
    @pytest.mark.parametrize(
        "column,expected",
        [("cargo_kg", "kg"), ("freight-KG", "kg"), ("Freight_Tonnes", "MT"),
         ("weight_in_MT", "MT"), ("TONNES_HANDLED", "MT")],
    )
    def test_unit_read_from_column_name(self, column, expected):
        """Regression: `_` is a word character, so `\\bkg\\b` never matched
        'cargo_kg' and the column was read as metric tonnes - a silent
        1000x error on every row."""
        assert infer_unit(column) == expected

    def test_unknown_column_falls_back_to_tonnes(self):
        assert infer_unit("cargo_quantity") == "MT"


class TestColumnMapping:
    def test_picks_the_airport_and_tonnage_columns(self):
        cols = ["S_No", "Airport_Name", "Freight_Tonnes", "Year"]
        assert _pick(cols, ("airport", "aerodrome")) == "Airport_Name"
        assert _pick(cols, ("freight", "cargo")) == "Freight_Tonnes"

    def test_exact_match_wins_over_substring(self):
        cols = ["airport_category", "airport"]
        assert _pick(cols, ("airport",)) == "airport"

    def test_returns_none_when_nothing_matches(self):
        assert _pick(["alpha", "beta"], ("airport",)) is None


class TestRelevanceClassifier:
    """A dataset must look like BOTH aviation AND cargo to be ingested."""

    def test_air_cargo_dataset_accepted(self):
        r = OgdResource("id", "Airport-wise Freight Handled", "cargo tonnage",
                        sector=["Aviation"], fields=["airport", "freight_tonnes"])
        assert r.relevance()[0]

    def test_rail_freight_rejected(self):
        """'freight' alone would wrongly catch railway goods traffic."""
        r = OgdResource("id", "Railway Goods Traffic", "freight by rail",
                        sector=["Railways"], fields=["tonnes"])
        assert not r.relevance()[0]

    def test_airport_passenger_data_rejected(self):
        """'airport' alone would wrongly catch passenger tables."""
        r = OgdResource("id", "Airport Passenger Movements", "passengers",
                        sector=["Aviation"], fields=["pax"])
        assert not r.relevance()[0]

    def test_evidence_is_returned_for_audit(self):
        r = OgdResource("id", "Air Cargo at Airports", "cargo",
                        sector=["Aviation"], fields=["freight"])
        ok, evidence = r.relevance()
        assert ok and evidence


class TestDataGovInParser:
    @staticmethod
    def _payload(records, title="Airport-wise Freight Handled 2023"):
        return json.dumps({
            "index_name": "abc", "title": title,
            "field": [{"name": k} for k in (records[0] if records else {})],
            "records": records,
        }).encode()

    @staticmethod
    def _doc():
        return SourceDocument(publisher=Publisher.DATA_GOV_IN,
                              source_url="https://api.data.gov.in/resource/abc")

    def test_maps_records_to_facts(self):
        payload = self._payload([
            {"Airport_Name": "DELHI", "Freight_Tonnes": "101651.2", "Year": "2023"},
            {"Airport_Name": "MUMBAI", "Freight_Tonnes": "81713.0", "Year": "2023"},
        ])
        r = DataGovInParser().parse(self._doc(), payload)
        assert len(r.facts) == 2
        delhi = next(f for f in r.facts if f.airport_name_raw == "DELHI")
        assert delhi.airport_iata == "DEL"
        assert delhi.tonnage_kg == pytest.approx(101_651_200.0)
        assert delhi.period == "2023-A"

    def test_kilogram_columns_are_not_rescaled(self):
        payload = self._payload([{"airport": "DELHI", "cargo_kg": "5000", "year": "2023"}])
        r = DataGovInParser().parse(self._doc(), payload)
        assert r.facts[0].tonnage_kg == 5000.0      # not 5,000,000

    def test_direction_inferred_from_title(self):
        payload = self._payload(
            [{"airport": "DELHI", "freight_tonnes": "10", "year": "2023"}],
            title="International Freight at Airports 2023")
        r = DataGovInParser().parse(self._doc(), payload)
        assert r.facts[0].direction is Direction.INTERNATIONAL

    def test_unmappable_schema_refuses_rather_than_guesses(self):
        payload = self._payload([{"alpha": "x", "beta": "1", "gamma": "2"}])
        r = DataGovInParser().parse(self._doc(), payload)
        assert r.facts == []
        assert any("cannot map" in w for w in r.warnings)

    def test_column_mapping_is_recorded_for_audit(self):
        payload = self._payload([{"Airport_Name": "DELHI", "Freight_Tonnes": "1", "Year": "2023"}])
        r = DataGovInParser().parse(self._doc(), payload)
        assert any("column_mapping=" in w for w in r.warnings)

    def test_empty_response_does_not_raise(self):
        r = DataGovInParser().parse(self._doc(), b'{"records": [], "field": []}')
        assert r.facts == [] and r.confidence == 0.0
