"""Unit tests for canonicalisation. Every case here came from real data."""

import pytest

from services.ingestion.normalise import (
    AirportResolver,
    fiscal_to_calendar_months,
    month_to_iso,
    name_variants,
    parse_number,
    strip_non_latin,
    to_kilograms,
)


class TestUnits:
    def test_metric_tonnes_to_kilograms(self):
        assert to_kilograms(175.7, "MT") == 175_700.0

    @pytest.mark.parametrize("unit", ["MT", "mt", "tonne", "tonnes", "T", "ton"])
    def test_tonne_spellings_agree(self, unit):
        assert to_kilograms(1, unit) == 1000.0

    def test_kilograms_pass_through(self):
        assert to_kilograms(500, "kg") == 500.0

    def test_unknown_unit_refuses(self):
        # Guessing a unit silently rescales every downstream number.
        with pytest.raises(ValueError):
            to_kilograms(1, "furlongs")


class TestNumberParsing:
    @pytest.mark.parametrize("raw", ["-", "--", "", "NA", "N/A", None])
    def test_null_markers_return_none(self, raw):
        """AAI writes '-' where a percentage is undefined."""
        assert parse_number(raw) is None

    def test_thousands_separators(self):
        assert parse_number("29,951.9") == 29951.9

    def test_negative(self):
        assert parse_number("-46.6") == -46.6


class TestPeriods:
    def test_month_to_iso(self):
        assert month_to_iso("April", 2026) == "2026-04"

    def test_abbreviated_month(self):
        assert month_to_iso("Feb", 2026) == "2026-02"

    def test_indian_fiscal_year_maps_to_calendar(self):
        assert fiscal_to_calendar_months("2026-2027") == ("2026-04", "2027-03")


class TestBilingualNames:
    def test_devanagari_is_stripped(self):
        assert strip_non_latin("अमृतसर AMRITSAR") == "AMRITSAR"

    def test_emptied_brackets_are_removed(self):
        """Regression: '(दिल्ली) DELHI' left '( ) DELHI', which resolved
        to nothing and dropped India's largest cargo airport."""
        assert strip_non_latin("( ) DELHI") == "DELHI"

    def test_variants_expose_both_halves_of_a_bracket(self):
        assert name_variants("ADAMPUR (JALANDHAR)") == [
            "ADAMPUR (JALANDHAR)", "ADAMPUR", "JALANDHAR"
        ]


class TestAirportResolution:
    @pytest.fixture(scope="class")
    @classmethod
    def resolver(cls):
        r = AirportResolver()
        r.load()
        return r

    @pytest.mark.parametrize(
        "name,expected",
        [("DELHI", "DEL"), ("MUMBAI", "BOM"), ("CHENNAI", "MAA"),
         ("KOLKATA", "CCU"), ("BENGALURU", "BLR"), ("HYDERABAD", "HYD")],
    )
    def test_major_hubs_resolve_to_the_right_airport(self, resolver, name, expected):
        """Regression: a city with several airports must resolve to the one
        the cargo actually goes through. 'DELHI' previously matched
        Safdarjung and 'HYDERABAD' matched Begumpet."""
        record, confidence, _ = resolver.resolve(name, country_hint="India")
        assert record is not None
        assert record["iata"] == expected
        assert confidence >= 0.86

    @pytest.mark.parametrize(
        "name,expected",
        [("KOCHI", "COK"), ("KOZHIKODE", "CCJ"), ("MYSURU", "MYQ"),
         ("THIRUVANANTHAPURAM", "TRV"), ("AYODHYA", "AYJ")],
    )
    def test_renamed_and_new_airports_resolve_via_alias(self, resolver, name, expected):
        """The bulk reference froze around 2017, so renamed cities and
        UDAN-era airports only resolve through the curated overlay."""
        record, _, _ = resolver.resolve(name, country_hint="India")
        assert record is not None and record["iata"] == expected

    def test_two_airports_in_one_city_stay_distinct(self, resolver):
        """'BENGALURU (HAL)' is not Kempegowda. Collapsing them onto BLR
        double-counts the city."""
        bial, _, _ = resolver.resolve("BENGALURU (BIAL)", country_hint="India")
        hal, _, _ = resolver.resolve("BENGALURU (HAL)", country_hint="India")
        assert bial["iata"] == "BLR"
        assert hal["iata"] == ""          # HAL has no commercial IATA code

    def test_nonsense_is_refused_not_guessed(self, resolver):
        _, confidence, method = resolver.resolve("ZZQQXX AIRPORT", country_hint="India")
        assert confidence < 0.86
        assert method == "unresolved"


class TestTransitionEraAirports:
    """A three-year backfill surfaces airport transitions that seven
    months of recent data never shows."""

    @pytest.fixture(scope="class")
    @classmethod
    def resolver(cls):
        r = AirportResolver()
        r.load()
        return r

    def test_rajkot_old_and_new_stay_distinct(self, resolver):
        """Both airports reported during the changeover, so merging them
        onto RAJ double-counts the city for the whole overlap."""
        old, _, _ = resolver.resolve("RAJKOT", country_hint="India")
        new, _, _ = resolver.resolve("RAJKOT (HIRASAR)", country_hint="India")
        assert old["iata"] == "RAJ"
        assert new["iata"] == "HSR"

    @pytest.mark.parametrize("written,expected", [
        # Releases before the city was renamed.
        ("BANGALORE (BIAL)", "BLR"),
        ("BENGALURU (BIAL)", "BLR"),
        # Older files omit the space before the bracket.
        ("HYDERABAD(BEGUMPET)", "BPM"),
        ("HYDERABAD (BEGUMPET)", "BPM"),
        ("KANPUR(Chakeri)", "KNU"),
    ])
    def test_older_spellings_resolve_the_same(self, resolver, written, expected):
        rec, conf, _ = resolver.resolve(written, country_hint="India")
        assert rec is not None and rec["iata"] == expected
        assert conf >= 0.86

    def test_bracket_spacing_is_normalised(self):
        """'HYDERABAD(BEGUMPET)' must produce the same variants as the
        spaced form, or the curated alias silently misses and the row
        falls back to the bare city."""
        assert name_variants("HYDERABAD(BEGUMPET)")[0] == "HYDERABAD (BEGUMPET)"
