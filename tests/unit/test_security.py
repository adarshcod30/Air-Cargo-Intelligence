"""Tests for the serving path's confinement (SRS NFR-6).

The semantic layer restricts which relations a generated query may name.
That is defence inside the application, and it is the useful first line.
These tests cover the second: the connection the API serves on can read
the allowlisted views and nothing else, so a query that ever escaped the
compiler still could not write, and could not read an unaudited row.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL_READONLY"),
    reason="DATABASE_URL_READONLY not set; apply db/readonly_role.sql",
)


@pytest.fixture(scope="module")
def ro_session():
    from services.api.deps import engine
    with Session(engine()) as s:
        yield s


class TestServingRoleIsConfined:
    def test_connects_as_the_readonly_role(self, ro_session):
        assert ro_session.execute(text("SELECT current_user")).scalar_one() == "aci_readonly"

    @pytest.mark.parametrize("view", [
        "v_cargo_fact", "v_trend", "v_anomaly", "v_forecast", "v_source",
    ])
    def test_can_read_every_allowlisted_view(self, ro_session, view):
        ro_session.execute(text(f"SELECT count(*) FROM {view}"))

    @pytest.mark.parametrize("table", [
        "fact_cargo_movement", "dim_airport", "dim_airline", "dim_period",
        "source_document", "trend", "anomaly", "forecast", "ingest_run",
    ])
    def test_cannot_read_the_base_tables(self, ro_session, table):
        """A view runs with its owner's rights, so reading through the
        views needs no grant on what is underneath - and withholding that
        grant is what stops an escaped query seeing an unaudited row."""
        # The guarantee is that the read is refused. Which exception the
        # driver raises for a permission error is its business, not ours.
        with pytest.raises(Exception):  # noqa: B017
            ro_session.execute(text(f"SELECT count(*) FROM {table}"))
        ro_session.rollback()

    @pytest.mark.parametrize("statement", [
        "DELETE FROM trend",
        "UPDATE fact_cargo_movement SET tonnage_kg = 0",
        "INSERT INTO dim_airport (airport_name) VALUES ('x')",
        "DROP VIEW v_trend",
        "CREATE TABLE evil (x int)",
        "TRUNCATE anomaly",
    ])
    def test_cannot_write_anything(self, ro_session, statement):
        with pytest.raises(Exception):  # noqa: B017
            ro_session.execute(text(statement))
        ro_session.rollback()


class TestNoEndpointReachesPastTheViews:
    def test_api_and_semantic_layer_only_read_views(self):
        """An endpoint querying a base table works for the owner and fails
        for the serving role, so it would break in production rather than
        in development. This keeps that from being discovered late."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        # Every base table, not a convenient subset: `insight` was
        # missing from this list and two endpoints reached straight past
        # the views, working for the owner and 500-ing for the serving
        # role.
        base_tables = (
            "fact_cargo_movement", "dim_airport", "dim_airline", "dim_period",
            "source_document", "ingest_run", "insight", "trend", "anomaly",
            "forecast",
        )
        offenders = []
        for path in (list((root / "services/api").glob("*.py"))
                     + list((root / "services/semantic").glob("*.py"))
                     + list((root / "services/reporting").glob("*.py"))):
            src = path.read_text()
            for table in base_tables:
                if re.search(rf"\bFROM\s+{table}\b", src, re.I):
                    offenders.append(f"{path.name} reads {table}")
        assert not offenders, offenders
