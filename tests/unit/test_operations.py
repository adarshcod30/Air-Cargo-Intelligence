"""Structural analytics over the cargo market."""

from __future__ import annotations

from services.analytics.operations import (
    attribute_growth,
    describe_correlation,
    herfindahl,
    pearson,
)
from services.ingestion.operating_metrics import classify


class TestAttribution:
    def test_contributions_sum_to_national_growth(self):
        """The property that makes it an attribution rather than a ranking.

        Each airport is measured against the national base, so the parts
        must reconstruct the whole. Measuring each against its own base
        would produce numbers that look similar and add up to nothing.
        """
        now = {"DEL": 110.0, "BOM": 85.0, "BLR": 60.0}
        then = {"DEL": 100.0, "BOM": 80.0, "BLR": 50.0}
        a = attribute_growth(now, then)
        assert a is not None
        total = sum(c.contribution_pp for c in a.contributors)
        assert abs(total - a.national_growth_pct) < 1e-9

    def test_a_small_airport_doubling_contributes_little(self):
        """Growth rate and contribution are different questions."""
        a = attribute_growth({"DEL": 1000.0, "TINY": 2.0}, {"DEL": 1000.0, "TINY": 1.0})
        tiny = next(c for c in a.contributors if c.entity_key == "TINY")
        assert tiny.change_mt == 1.0
        assert tiny.contribution_pp < 0.2

    def test_declines_are_reported_as_negative(self):
        a = attribute_growth({"CCU": 90.0}, {"CCU": 100.0})
        assert a.contributors[0].contribution_pp < 0

    def test_no_base_returns_none(self):
        assert attribute_growth({"A": 5.0}, {}) is None


class TestConcentration:
    def test_monopoly_is_maximally_concentrated(self):
        h = herfindahl([100.0])
        assert h["hhi"] == 10000
        assert h["interpretation"] == "highly concentrated"

    def test_effective_n_recovers_the_number_of_equal_players(self):
        """Four equal shares should read as about four competitors."""
        h = herfindahl([25.0, 25.0, 25.0, 25.0])
        assert abs(h["effective_n"] - 4.0) < 0.05

    def test_empty_traffic_says_so(self):
        assert herfindahl([0.0, 0.0])["hhi"] is None


class TestCorrelation:
    def test_refuses_too_few_pairs(self):
        """Three points can produce a confident-looking number from noise."""
        assert pearson([1, 2, 3], [1, 2, 3]) is None

    def test_refuses_a_constant_series(self):
        assert pearson([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None

    def test_detects_a_clean_relationship(self):
        r = pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
        assert r is not None and r > 0.99

    def test_missing_values_are_dropped_not_zeroed(self):
        r = pearson([1, 2, None, 4, 5], [2, 4, 6, 8, 10])
        assert r is not None and r > 0.99

    def test_description_declines_to_guess(self):
        assert "too few" in describe_correlation(None)


class TestMetricClassification:
    def test_tonne_kilometres_are_not_read_as_tonnage(self):
        """The distinction the cargo parser was right to make.

        `ton_kms_performed_million___freight` and `cargo_carried_ton___freight`
        both contain freight and ton; one is a distance-weighted measure and
        one is a mass, and conflating them corrupts both.
        """
        assert classify("ton_kms_performed_million___freight").metric == "ftk_million"
        assert classify("cargo_carried_ton___freight").metric == "freight_tonnes"

    def test_capacity_and_output_are_distinguished(self):
        assert classify("available_tonne_kilometers_million_").metric == "atk_million"
        assert classify("ton_kms_performed_million___total").metric == "ttk_million"

    def test_unrelated_columns_are_ignored(self):
        assert classify("_sl_no_") is None
        assert classify("aircraft_type") is None
