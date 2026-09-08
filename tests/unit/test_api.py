"""End-to-end API tests against the live warehouse.

Skipped when no database is configured, so the suite still runs on a
machine that has only checked the code out.
"""

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    from services.api.main import app
    return TestClient(app)


class TestMeta:
    def test_health_reports_real_counts(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["facts"] > 0

    def test_semantic_layer_is_discoverable(self, client):
        """A client should be able to learn what it may ask for."""
        body = client.get("/api/v1/semantic").json()
        assert "tonnage_mt" in body["metrics"]
        assert "v_cargo_fact" in body["sources"]

    def test_sources_endpoint_lists_publishers(self, client):
        body = client.get("/api/v1/sources").json()
        assert body["publishers"]
        assert all("publisher" in p for p in body["publishers"])


class TestAirports:
    def test_rankings_return_rows_with_citations(self, client):
        body = client.get("/api/v1/airports/rankings?limit=5").json()
        assert body["row_count"] > 0
        assert body["explanation"]
        assert body["citations"], "a figure without its source is not shippable"

    def test_growth_ranking_excludes_trivial_volumes(self, client):
        """A 4-tonne airport posting 486% growth is noise, not news."""
        body = client.get(
            "/api/v1/airports/rankings?order_by=growth_yoy_pct&limit=10"
        ).json()
        assert all(r["tonnage_mt"] >= 100 for r in body["rows"])

    def test_invalid_direction_is_refused(self, client):
        assert client.get("/api/v1/airports/rankings?direction=SIDEWAYS").status_code == 422

    def test_trend_for_one_airport(self, client):
        body = client.get("/api/v1/airports/trend?iata=DEL").json()
        assert body["row_count"] > 0
        assert all(r["airport_iata"] == "DEL" for r in body["rows"])


class TestAirlines:
    def test_share_excludes_industry_totals_by_default(self, client):
        """'All Scheduled Indian Airlines' is a total, not a carrier;
        including it beside carriers double-counts the market."""
        body = client.get("/api/v1/airlines/share?limit=20").json()
        names = {r["airline_name"] for r in body["rows"]}
        assert not any(n.lower().startswith("all ") for n in names)

    def test_aggregates_can_be_requested_explicitly(self, client):
        body = client.get(
            "/api/v1/airlines/share?include_aggregates=true&limit=20"
        ).json()
        assert body["row_count"] > 0


class TestIntelligence:
    def test_anomalies_have_a_baseline_to_compare_against(self, client):
        body = client.get("/api/v1/anomalies?limit=10").json()
        for row in body["rows"]:
            assert row["expected_mt"] is not None

    def test_forecasts_always_carry_an_interval(self, client):
        """A point estimate alone invites more confidence than these
        short series support."""
        body = client.get("/api/v1/forecasts?limit=10").json()
        for row in body["rows"]:
            assert row["lower_mt"] is not None and row["upper_mt"] is not None
            assert row["lower_mt"] <= row["predicted_mt"] <= row["upper_mt"]


class TestChat:
    @pytest.mark.parametrize("question", [
        "Show the top 5 cargo airports by growth",
        "Which airlines carry the most cargo?",
        "What anomalies have been detected?",
        "Predict cargo demand next quarter",
        "Where does this data come from?",
    ])
    def test_every_answer_is_grounded(self, client, question):
        body = client.post("/api/v1/chat/query", json={"question": question}).json()
        assert body["grounded"] is True, body["answer"]
        assert body["understood_as"]

    def test_out_of_scope_question_is_declined_not_guessed(self, client):
        body = client.post(
            "/api/v1/chat/query", json={"question": "What is the capital of France?"}
        ).json()
        assert body["intent"] == "unknown"
        assert "could not tell which" in body["answer"]

    def test_airport_code_is_validated_not_assumed(self, client):
        """Uppercasing the question made every three-letter word a
        candidate, so 'How has DEL changed' resolved to airport 'HOW'."""
        body = client.post(
            "/api/v1/chat/query", json={"question": "How has DEL changed over time?"}
        ).json()
        assert "DEL" in body["understood_as"]
        assert body["row_count"] if "row_count" in body else body["rows"]

    def test_a_too_short_question_is_rejected(self, client):
        assert client.post("/api/v1/chat/query", json={"question": "a"}).status_code == 422


class TestRankingsCompareLikeWithLike:
    """A ranking that mixes period kinds looks authoritative and is
    meaningless. Without a period scope the endpoint summed across every
    period held, so Eurostat's annual figure for Frankfurt (3.8m MT for a
    year) outranked Delhi's monthly one (105k MT for a month)."""

    def test_default_ranking_is_scoped_to_one_period(self, client):
        body = client.get("/api/v1/airports/rankings?limit=20").json()
        periods = {r.get("period") for r in body["rows"] if "period" in r}
        assert len(periods) <= 1
        assert "period=" in body["explanation"]

    def test_annual_figures_do_not_outrank_monthly_ones(self, client):
        """Frankfurt's yearly total must not head a monthly league table."""
        body = client.get("/api/v1/airports/rankings?limit=10").json()
        codes = [r["airport_iata"] for r in body["rows"]]
        assert codes and codes[0] == "DEL", codes

    def test_an_explicit_period_is_respected(self, client):
        body = client.get("/api/v1/airports/rankings?period=2026-01&limit=5").json()
        assert "2026-01" in body["explanation"]
        assert body["row_count"] > 0

    def test_period_kind_is_filterable(self):
        """So a caller can compare annual with annual when they want to."""
        from services.semantic.registry import DIMENSIONS, FILTERS
        assert "period_kind" in FILTERS and "period_kind" in DIMENSIONS
