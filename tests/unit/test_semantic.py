"""Tests for the semantic layer and its security boundary.

The compiler is what makes "the model never computes a number"
enforceable. A caller supplies names, never SQL, and every name is
looked up in the registry before anything is emitted.
"""

import pytest

from services.semantic.compiler import (
    MAX_LIMIT,
    QueryRejected,
    QuerySpec,
    compile_query,
)
from services.semantic.nl import _verify_grounded, classify
from services.semantic.registry import ALLOWED_SOURCES, DIMENSIONS, METRICS, describe


class TestRegistry:
    def test_every_metric_carries_a_unit(self):
        assert all(m.unit for m in METRICS.values())

    def test_grain_specific_dimensions_are_scoped(self):
        """An airline name means nothing at airport grain, and vice versa."""
        assert DIMENSIONS["airline_name"].grains == frozenset({"AIRLINE"})
        assert DIMENSIONS["airport_iata"].grains == frozenset({"AIRPORT"})

    def test_registry_is_discoverable(self):
        d = describe()
        assert d["metrics"] and d["dimensions"] and d["filters"]
        assert set(d["sources"]) == ALLOWED_SOURCES


class TestCompilerRejectsWhatItDoesNotKnow:
    """Each case is an attempt to reach past the registry."""

    def test_sql_injection_in_a_metric_name(self):
        with pytest.raises(QueryRejected, match="unknown metric"):
            compile_query(QuerySpec(metrics=["tonnage_mt; DROP TABLE fact_cargo_movement"]))

    def test_injection_in_a_dimension_name(self):
        with pytest.raises(QueryRejected, match="unknown dimension"):
            compile_query(QuerySpec(dimensions=["airport_iata; DELETE FROM trend"]))

    def test_base_tables_are_not_reachable(self):
        """Only the allowlisted views, never the tables behind them."""
        with pytest.raises(QueryRejected, match="unknown source"):
            compile_query(QuerySpec(source="fact_cargo_movement"))

    def test_unknown_filter_is_refused(self):
        with pytest.raises(QueryRejected, match="unknown filter"):
            compile_query(QuerySpec(filters={"; DROP TABLE": "x"}))

    def test_dimension_must_match_the_grain(self):
        with pytest.raises(QueryRejected, match="does not apply at grain"):
            compile_query(QuerySpec(dimensions=["airline_name"],
                                    filters={"grain": "AIRPORT"}))

    def test_cannot_order_by_something_not_selected(self):
        with pytest.raises(QueryRejected, match="cannot order by"):
            compile_query(QuerySpec(metrics=["tonnage_mt"], order_by="secret_column"))

    def test_at_least_one_metric_required(self):
        with pytest.raises(QueryRejected):
            compile_query(QuerySpec(metrics=[]))


class TestCompilerOutput:
    def test_values_are_bound_never_interpolated(self):
        """A value must never reach the SQL text itself."""
        q = compile_query(QuerySpec(
            metrics=["tonnage_mt"],
            filters={"grain": "AIRPORT", "airport_iata": "DEL'; DROP TABLE x;--"},
        ))
        assert "DROP TABLE" not in q.sql
        assert "DEL'; DROP TABLE x;--" in q.params.values()

    def test_limit_is_capped(self):
        q = compile_query(QuerySpec(metrics=["tonnage_mt"], limit=10_000_000))
        assert f"LIMIT {MAX_LIMIT}" in q.sql

    def test_limit_has_a_floor(self):
        q = compile_query(QuerySpec(metrics=["tonnage_mt"], limit=0))
        assert "LIMIT 1" in q.sql

    def test_boolean_filter_is_a_switch_not_a_comparison(self):
        on = compile_query(QuerySpec(metrics=["tonnage_mt"],
                                     filters={"grain": "AIRLINE",
                                              "exclude_aggregate_airlines": True}))
        off = compile_query(QuerySpec(metrics=["tonnage_mt"],
                                      filters={"grain": "AIRLINE",
                                               "exclude_aggregate_airlines": False}))
        assert "airline_is_aggregate" in on.sql
        assert "airline_is_aggregate" not in off.sql

    def test_grouping_follows_the_dimensions(self):
        q = compile_query(QuerySpec(metrics=["tonnage_mt"],
                                    dimensions=["airport_iata", "period"],
                                    filters={"grain": "AIRPORT"}))
        assert "GROUP BY 1, 2" in q.sql

    def test_explanation_is_human_readable(self):
        q = compile_query(QuerySpec(metrics=["tonnage_mt"],
                                    dimensions=["airport_iata"],
                                    filters={"grain": "AIRPORT"}, limit=5))
        text = q.explain()
        assert "tonnage_mt" in text and "airport_iata" in text and "limit 5" in text


class TestIntentClassification:
    @pytest.mark.parametrize("question,expected", [
        ("Show the top 5 cargo airports by growth", "airport_ranking"),
        ("Which airlines carry the most cargo?", "airline_ranking"),
        ("What anomalies have been detected?", "anomaly_list"),
        ("Predict cargo demand next quarter", "forecast_list"),
        ("Where does this data come from?", "source_list"),
    ])
    def test_recognised_questions(self, question, expected):
        intent = classify(question)
        assert intent is not None and intent.name == expected

    def test_out_of_scope_question_is_not_guessed(self):
        """Answering a question it does not understand is worse than
        declining to."""
        assert classify("What is the capital of France?") is None


class TestGroundingVerification:
    """The check that turns the central rule into something enforced."""

    def test_figures_present_in_the_rows_pass(self):
        assert _verify_grounded("moved 105,642.2 MT", [{"tonnage_mt": 105642.2}])

    def test_an_invented_figure_is_caught(self):
        assert not _verify_grounded("moved 999,999.9 MT", [{"tonnage_mt": 105642.2}])

    def test_decimal_values_are_matched(self):
        """Decimal is neither int nor float, and an isinstance check
        against those two skipped every SQL-rounded value - which marked
        perfectly grounded answers unverifiable."""
        from decimal import Decimal
        assert _verify_grounded("137.5 MT", [{"observed_mt": Decimal("137.5")}])

    def test_period_labels_are_not_read_as_figures(self):
        """'2026-07' was being parsed as the number -7, which then failed
        to appear in the rows."""
        assert _verify_grounded("in 2026-07 it moved 137.5 MT",
                                [{"observed_mt": 137.5}])

    def test_fiscal_and_annual_labels_too(self):
        assert _verify_grounded("in 2015-FY and 2023-A it moved 12.5 MT",
                                [{"v": 12.5}])

    def test_an_answer_with_no_rows_is_vacuously_grounded(self):
        assert _verify_grounded("I have no data for that.", [])
