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

    def test_no_tonnage_column_refuses_rather_than_guesses(self):
        payload = self._payload([{"alpha": "x", "beta": "1", "gamma": "2"}])
        r = DataGovInParser().parse(self._doc(), payload)
        assert r.facts == []
        assert any("no tonnage column" in w for w in r.warnings)

    def test_unidentifiable_airline_refuses(self):
        """An airline-level dataset whose title names no carrier must be
        refused, not attributed to an unknown airline."""
        payload = self._payload([{"cargo_tonnes": "10", "year": "2023"}],
                                title="Yearly Domestic Traffic by Air")
        r = DataGovInParser().parse(self._doc(), payload)
        assert r.facts == []
        assert any("cannot identify the airline" in w for w in r.warnings)

    def test_column_mapping_is_recorded_as_a_note_not_a_warning(self):
        """Recording the mapping is an audit trail, not a problem - counting
        it as a warning docked the confidence score below the floor and
        quarantined flawless extractions."""
        payload = self._payload([{"Airport_Name": "DELHI", "Freight_Tonnes": "1", "Year": "2023"}])
        r = DataGovInParser().parse(self._doc(), payload)
        assert any("column_mapping=" in n for n in r.notes)
        assert not any("column_mapping=" in w for w in r.warnings)

    def test_empty_response_does_not_raise(self):
        r = DataGovInParser().parse(self._doc(), b'{"records": [], "field": []}')
        assert r.facts == [] and r.confidence == 0.0


class TestAirlineGrain:
    """The OGD aviation catalogue is airline-level: 141 of its cargo-bearing
    datasets carry no airport column at all, and name the carrier only in
    the dataset title."""

    @staticmethod
    def _payload(records, title):
        return json.dumps({"index_name": "x", "title": title,
                           "field": [{"name": k} for k in records[0]],
                           "records": records}).encode()

    @staticmethod
    def _doc():
        return SourceDocument(publisher=Publisher.DATA_GOV_IN,
                              source_url="https://api.data.gov.in/resource/x")

    def test_airline_extracted_from_title(self):
        from services.common.models import Grain
        payload = self._payload(
            [{"_year": "2015-16", "cargo_carried_ton___total": "1234"}],
            "Details of Annual Traffic and Operating Statistics on Domestic "
            "Scheduled Services of Air Costa from 2007-08 to 2015-16")
        r = DataGovInParser().parse(self._doc(), payload)
        assert len(r.facts) == 1
        f = r.facts[0]
        assert f.grain is Grain.AIRLINE
        assert f.airline == "Air Costa"
        assert f.tonnage_kg == 1_234_000.0

    def test_last_of_in_title_names_the_carrier(self):
        """These titles contain several 'of's; the last introduces the
        airline. Matching the first yielded 'Annual Traffic and Operating
        Statistics' as the carrier name."""
        from services.ingestion.parsers.datagovin import _airline_from_title
        t = ("Details of Annual Traffic and Operating Statistics on Domestic "
             "Scheduled Services of Air Costa from 2007-08")
        assert _airline_from_title(t) == "Air Costa"

    def test_rate_columns_are_not_read_as_mass(self):
        """'ton_kms_performed' and 'percentage growth' both contain cargo
        hint words but are a distance-weighted measure and a percentage."""
        cols = ["_year", "ton_kms_performed_million___freight",
                "pax_load_factor_", "cargo_carried_ton___total"]
        assert DataGovInParser._pick_tonnage(cols) == "cargo_carried_ton___total"

    def test_total_preferred_over_components(self):
        """Summing freight and mail alongside their total double-counts."""
        cols = ["cargo_carried_ton___freight", "cargo_carried_ton___mail",
                "cargo_carried_ton___total"]
        assert DataGovInParser._pick_tonnage(cols) == "cargo_carried_ton___total"

    def test_fiscal_year_not_read_as_a_month(self):
        """'2007-08' is the Indian fiscal year, not August 2007. Reading it
        as a month files a whole year of cargo under one wrong month."""
        assert DataGovInParser._row_period({"y": "2007-08"}, "y") == "2007-FY"
        assert DataGovInParser._row_period({"y": "2023-05"}, "y") == "2023-05"

    def test_airline_extraction_scores_above_the_floor(self):
        """A flawless airline extraction must clear the confidence floor;
        it previously scored 0.55 because it had no airport code."""
        payload = self._payload(
            [{"_year": "2015-16", "cargo_carried_ton___total": str(i)} for i in range(9)],
            "Cargo Traffic of Air India from 2007-08 to 2015-16")
        r = DataGovInParser().parse(self._doc(), payload)
        assert r.confidence >= 0.60, r.summary()


class TestAirlineNameQuality:
    """Two ways the carrier name went wrong on real catalogue titles."""

    @pytest.mark.parametrize(
        "title,expected",
        [
            # 'by' introduces the carrier as often as 'of' does. Matching
            # only 'of' yielded 'Aircraft by Air India'.
            ("Fleet Strength and Utilisation of Aircraft by Air India from 2007-08", "Air India"),
            ("Selected Traffic Coefficients of Scheduled Commercial Traffic "
             "Carried by Vistara from 2008-09", "Vistara"),
            ("Details of Annual Traffic and Operating Statistics on Domestic "
             "Scheduled Services of Air Costa from 2007-08", "Air Costa"),
            ("Employees Productivity of Blue Dart from 2012-13", "Blue Dart"),
        ],
    )
    def test_carrier_not_a_clause_fragment(self, title, expected):
        from services.ingestion.parsers.datagovin import _airline_from_title
        assert _airline_from_title(title) == expected

    def test_description_words_are_never_a_carrier_name(self):
        from services.ingestion.parsers.datagovin import _airline_from_title
        assert _airline_from_title("Report of Annual Traffic Statistics from 2010") is None

    @pytest.mark.parametrize("name", [
        "All Scheduled Indian Airlines", "All Scheduled Private Airlines",
        "All Scheduled National Airlines", "Private Carriers",
    ])
    def test_industry_totals_are_flagged_as_aggregates(self, name):
        """Adding an industry total to per-carrier rows double-counts the
        whole market. The totals are useful, so they are labelled rather
        than discarded."""
        from services.ingestion.parsers.datagovin import _AGGREGATE_AIRLINE
        assert _AGGREGATE_AIRLINE.match(name)

    @pytest.mark.parametrize("name", ["Air India", "Indigo", "Blue Dart", "Vistara"])
    def test_real_carriers_are_not_flagged(self, name):
        from services.ingestion.parsers.datagovin import _AGGREGATE_AIRLINE
        assert not _AGGREGATE_AIRLINE.match(name)

    def test_aggregate_flag_reaches_the_fact(self):
        payload = json.dumps({
            "index_name": "x",
            "title": "Scheduled Cargo Traffic of All Scheduled Indian Airlines from 2007-08",
            "field": [{"name": "_year"}, {"name": "cargo_carried_ton___total"}],
            "records": [{"_year": "2015-16", "cargo_carried_ton___total": "100"}],
        }).encode()
        doc = SourceDocument(publisher=Publisher.DATA_GOV_IN,
                             source_url="https://api.data.gov.in/resource/x")
        r = DataGovInParser().parse(doc, payload)
        assert r.facts and r.facts[0].is_aggregate is True


class TestFiscalMonthRows:
    """These tables put a fiscal-year header above bare month names:

        year_month
        ----------
        2015-16      <- header
        APR
        MAY

    A month with no year of its own inherits the header's. Without that
    every month fell back to the title's year and twelve rows collapsed
    into a single annual period, losing the monthly dimension entirely.
    """

    def test_header_row_yields_its_first_year(self):
        from services.ingestion.parsers.datagovin import _header_year
        assert _header_year({"y": "2015-16"}, "y") == 2015
        assert _header_year({"y": "2015"}, "y") == 2015
        assert _header_year({"y": "APR"}, "y") is None

    @pytest.mark.parametrize("month,expected", [
        ("APR", "2015-04"), ("DEC", "2015-12"),
        # An Indian fiscal year runs April to March, so the calendar year
        # rolls over at January.
        ("JAN", "2016-01"), ("MAR", "2016-03"),
    ])
    def test_month_inherits_the_fiscal_year_correctly(self, month, expected):
        assert DataGovInParser._row_period({"y": month}, "y", 2015) == expected

    def test_month_without_a_header_is_refused(self):
        """Guessing the year would misdate the whole series."""
        assert DataGovInParser._row_period({"y": "APR"}, "y", None) is None

    def test_monthly_rows_produce_distinct_periods(self):
        payload = json.dumps({
            "index_name": "x",
            "title": "Monthly Traffic and Operating Statistics of Jetlite from 2015-16",
            "field": [{"name": "year_month"}, {"name": "cargo_carried_ton___total"}],
            "records": [
                {"year_month": "2015-16", "cargo_carried_ton___total": ""},
                {"year_month": "APR", "cargo_carried_ton___total": "100"},
                {"year_month": "MAY", "cargo_carried_ton___total": "110"},
                {"year_month": "JAN", "cargo_carried_ton___total": "120"},
            ],
        }).encode()
        doc = SourceDocument(publisher=Publisher.DATA_GOV_IN,
                             source_url="https://api.data.gov.in/resource/x")
        r = DataGovInParser().parse(doc, payload)
        assert {f.period for f in r.facts} == {"2015-04", "2015-05", "2016-01"}


class TestAircraftSpecColumnsRejected:
    """A fleet table's "SIZE AV. PAYLOAD CAPACITY (TONNES)" contains the
    word tonnes but describes what an aircraft *could* carry, not what it
    did. Ingesting it as cargo moved is simply a wrong number."""

    @pytest.mark.parametrize("column", [
        "size_av_payload_capacity_tonnes_",
        "available_tonne_kilometers_million_",
        "size_no_of_installed_pax_seats",
        "size_av_m_c_t_om_weight_in_tonne",
    ])
    def test_specification_columns_are_not_tonnage(self, column):
        assert DataGovInParser._pick_tonnage([column, "aircraft_type"]) is None

    def test_fleet_table_yields_no_tonnage_column(self):
        cols = ["aircraft_type", "number_of_aircraft_acquired",
                "size_av_payload_capacity_tonnes_"]
        assert DataGovInParser._pick_tonnage(cols) is None

    def test_real_cargo_column_still_chosen(self):
        cols = ["year_month", "cargo_carried_ton___total",
                "available_tonne_kilometers_million_"]
        assert DataGovInParser._pick_tonnage(cols) == "cargo_carried_ton___total"
