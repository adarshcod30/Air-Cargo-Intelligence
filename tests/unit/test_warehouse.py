"""Tests for warehouse period parsing and schema guarantees.

The schema encodes three rules the database enforces so application code
cannot forget them: provenance is mandatory, tonnage is non-negative, and
the natural key is unique.
"""

import pytest

from services.warehouse.loader import parse_period
from services.warehouse.schema import Base, CargoFactRow, SourceDocument


class TestPeriodParsing:
    """Three kinds of period coexist and must stay distinguishable."""

    def test_calendar_month(self):
        p = parse_period("2026-04")
        assert p["period_kind"] == "MONTH"
        assert (p["calendar_year"], p["calendar_month"]) == (2026, 4)

    def test_indian_fiscal_year(self):
        """'2015-FY' is a fiscal year, not a month. Flattening it into a
        date would misdate a whole year of cargo."""
        p = parse_period("2015-FY")
        assert p["period_kind"] == "FISCAL"
        assert p["fiscal_year_start"] == 2015
        assert p["calendar_month"] is None

    def test_plain_year(self):
        p = parse_period("2023-A")
        assert p["period_kind"] == "ANNUAL"
        assert p["calendar_year"] == 2023

    def test_unparseable_label_does_not_raise(self):
        assert parse_period("nonsense")["period_kind"] == "ANNUAL"

    def test_sort_key_orders_all_three_kinds_on_one_axis(self):
        keys = [parse_period(x)["sort_key"]
                for x in ("2023-01", "2023-A", "2024-01")]
        assert keys == sorted(keys)

    def test_fiscal_year_sorts_inside_its_own_year(self):
        fy = parse_period("2015-FY")["sort_key"]
        assert parse_period("2015-01")["sort_key"] < fy < parse_period("2016-01")["sort_key"]


class TestSchemaGuarantees:
    """Guarantees that must live in the database, not in a convention."""

    def test_provenance_is_not_nullable(self):
        """Source citation is the product's central promise; a nullable
        foreign key would make it a hope."""
        assert CargoFactRow.__table__.c.source_document_id.nullable is False

    def test_tonnage_is_non_negative(self):
        checks = {c.name for c in CargoFactRow.__table__.constraints
                  if c.__class__.__name__ == "CheckConstraint"}
        assert "ck_fact_cargo_movement_tonnage_non_negative" in checks

    def test_grain_requires_its_own_dimension(self):
        checks = {c.name for c in CargoFactRow.__table__.constraints
                  if c.__class__.__name__ == "CheckConstraint"}
        assert "ck_fact_cargo_movement_grain_has_its_dimension" in checks

    def test_natural_key_includes_measure(self):
        """One carrier can publish several distinct series for the same
        period and direction; without `measure` they overwrite."""
        uq = next(c for c in CargoFactRow.__table__.constraints
                  if c.name == "fact_natural_key")
        assert "measure" in {c.name for c in uq.columns}

    def test_natural_key_treats_nulls_as_equal(self):
        """Postgres treats NULLs as distinct by default, so an
        airline-grain row (airport_id NULL) would never conflict with
        itself and would duplicate on every re-run."""
        uq = next(c for c in CargoFactRow.__table__.constraints
                  if c.name == "fact_natural_key")
        assert uq.dialect_options["postgresql"]["nulls_not_distinct"] is True

    def test_stored_urls_cannot_contain_a_live_key(self):
        checks = {c.name for c in SourceDocument.__table__.constraints
                  if c.__class__.__name__ == "CheckConstraint"}
        assert "ck_source_document_no_credential_in_url" in checks

    @pytest.mark.parametrize("table", ["fact_cargo_movement", "dim_airport",
                                       "dim_airline", "dim_period",
                                       "source_document", "trend", "anomaly",
                                       "forecast", "insight", "ingest_run"])
    def test_expected_tables_exist(self, table):
        assert table in Base.metadata.tables
