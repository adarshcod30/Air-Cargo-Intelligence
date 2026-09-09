"""Analytics agent: turns warehouse facts into trends, anomalies, forecasts.

Same contract as every other agent in this pipeline - a goal, a set of
tools, a budget, and a recorded trace - so an analytics run is auditable
in exactly the way an ingestion run is.

It writes rows back into the warehouse rather than computing on request.
Storing results makes them reproducible, lets an insight cite them later,
and keeps the serving layer a thin read path.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.agents.base import Agent, Decision
from services.agents.policy import HeuristicPolicy
from services.analytics import anomaly as anomaly_mod
from services.analytics import forecast as forecast_mod
from services.analytics.trend import compute_trend
from services.common.logging import get_logger
from services.common.models import ToolCall
from services.warehouse.loader import batched_upsert, get_engine
from services.warehouse.queries import load_series, national_totals, period_ids
from services.warehouse.schema import Anomaly, Forecast, Trend

log = get_logger(__name__)


class AnalyticsAgent(Agent):
    name = "analytics"

    def __init__(self, engine=None) -> None:
        # Deterministic on purpose. This pipeline has a fixed dependency
        # chain (trends feed anomalies feed forecasts feed persist), so there is
        # no shape for a model to discover and nothing for it to decide.
        # Letting it choose cost a run: it went from detect_anomalies
        # straight to persist, skipping the forecast step, and stored zero
        # forecasts without failing. The plan cannot do that - it refuses to
        # persist until every prior step is done.
        #
        # Discovery and extraction keep the model policy, where document
        # shapes genuinely vary and adapting is the point.
        super().__init__(policy=HeuristicPolicy(self._plan))
        self.engine = engine or get_engine()
        self._register_tools()

    # ----------------------------------------------------------- tools --

    def _register_tools(self) -> None:
        @self.tool("load_series", "Read every entity's time series from the warehouse.")
        def load() -> int:
            with Session(self.engine) as s:
                series = load_series(s, min_points=3)
                self.context["series"] = series
                self.context["totals"] = national_totals(s)
                self.context["period_ids"] = period_ids(s)
            return len(series)

        @self.tool("compute_trends", "Growth, share and share-shift per series.")
        def trends() -> int:
            series = self.context["series"]
            totals = self.context["totals"]
            rows = []
            for sr in series:
                tot = [totals.get((p, sr.direction)) or 0.0 for p in sr.periods]
                for pt in compute_trend(sr.periods, sr.sort_keys, sr.values, tot,
                                        prior_year=sr.prior_year):
                    rows.append((sr, pt))
            self.context["trend_rows"] = rows
            return len(rows)

        @self.tool("detect_anomalies", "Flag unusual movements in each series.")
        def anomalies() -> int:
            """One alert per event, not one per direction it shows up in.

            Detection runs on every series, but TOTAL is INTERNATIONAL plus
            DOMESTIC, so a domestic spike necessarily moves TOTAL as well.
            Storing each was reporting one event up to three times: 172 of
            240 flagged entity-periods appeared in more than one direction.

            The surviving alert is the direction with the largest deviation,
            which is the one that explains the movement. The others are not
            evidence of anything the first does not already say.
            """
            candidates: dict[tuple, tuple] = {}
            for sr in self.context["series"]:
                for a in anomaly_mod.detect(sr.periods, sr.values):
                    if not anomaly_mod.is_material(a):
                        continue
                    key = (sr.grain, sr.entity_key, a.period)
                    best = candidates.get(key)
                    if best is None or abs(a.deviation_pct or 0) > abs(best[1].deviation_pct or 0):
                        candidates[key] = (sr, a)
            rows = list(candidates.values())
            self.context["anomaly_rows"] = rows
            return len(rows)

        @self.tool("build_forecasts", "Forecast each series, backtested.")
        def forecasts() -> int:
            rows = []
            for sr in self.context["series"]:
                # Forecasting an aggregate of aggregates is noise; only
                # series with enough history are worth projecting.
                if len(sr) < 8:
                    continue
                for f in forecast_mod.forecast(sr.periods, sr.values, horizon=3):
                    rows.append((sr, f))
            self.context["forecast_rows"] = rows
            return len(rows)

        @self.tool("persist", "Write trends, anomalies and forecasts to the warehouse.")
        def persist() -> dict:
            pid = self.context["period_ids"]

            trends = [
                {
                    "grain": sr.grain, "entity_key": sr.entity_key[:64],
                    "direction": sr.direction, "period_id": pid[pt.period],
                    "tonnage_kg": pt.tonnage_kg, "yoy_pct": pt.yoy_pct,
                    "mom_pct": pt.mom_pct, "cagr_pct": pt.cagr_pct,
                    "share_of_total": pt.share_of_total,
                    "share_shift_pp": pt.share_shift_pp,
                }
                for sr, pt in self.context.get("trend_rows", [])
                if pt.period in pid
            ]
            anomalies = [
                {
                    "grain": sr.grain, "entity_key": sr.entity_key[:64],
                    "direction": sr.direction, "period_id": pid[a.period],
                    "observed_kg": a.observed_kg, "expected_kg": a.expected_kg,
                    "deviation_pct": a.deviation_pct, "z_score": a.z_score,
                    "method": a.method, "severity": a.severity,
                    "evidence_fact_ids": f"{sr.grain}:{sr.entity_key}:{a.period}",
                }
                for sr, a in self.context.get("anomaly_rows", [])
                if a.period in pid
            ]
            forecasts = [
                {
                    "grain": sr.grain, "entity_key": sr.entity_key[:64],
                    "direction": sr.direction, "period_label": f.period_label,
                    "horizon": f.horizon, "predicted_kg": f.predicted_kg,
                    "lower_kg": f.lower_kg, "upper_kg": f.upper_kg,
                    "model": f.model, "backtest_mape": f.backtest_mape,
                }
                for sr, f in self.context.get("forecast_rows", [])
            ]

            with Session(self.engine) as s:
                # Anomalies and forecasts are wholly derived: a run computes
                # what is true now, so what it does not produce is no longer
                # true. Upserting alone made the tables the union of every
                # run ever made, so a month that stopped being anomalous
                # stayed flagged forever and a forecast withdrawn for poor
                # backtest error kept being served.
                #
                # Insights carry a foreign key to the anomaly they explain,
                # and are themselves regenerated each run, so they are
                # removed alongside the anomalies that no longer exist.
                self._retire_superseded(s, anomalies, forecasts)

                written = {
                    "trend": batched_upsert(
                        s, Trend, "trend_natural_key",
                        ["tonnage_kg", "yoy_pct", "mom_pct", "cagr_pct",
                         "share_of_total", "share_shift_pp"],
                        trends,
                        lambda r: (r["grain"], r["entity_key"], r["direction"],
                                   r["period_id"]),
                    ),
                    "anomaly": batched_upsert(
                        s, Anomaly, "anomaly_natural_key",
                        ["observed_kg", "z_score", "severity"],
                        anomalies,
                        lambda r: (r["grain"], r["entity_key"], r["direction"],
                                   r["period_id"], r["method"]),
                    ),
                    "forecast": batched_upsert(
                        s, Forecast, "forecast_natural_key",
                        ["predicted_kg", "lower_kg", "upper_kg", "backtest_mape"],
                        forecasts,
                        lambda r: (r["grain"], r["entity_key"], r["direction"],
                                   r["period_label"], r["model"]),
                    ),
                }
                s.commit()
            self.context["written"] = written
            return written

    @staticmethod
    def _retire_superseded(s, anomalies: list[dict], forecasts: list[dict]) -> None:
        """Remove derived rows this run did not produce.

        Keyed on the same natural keys the upsert uses, so "not produced"
        means exactly "would not be written now".
        """
        from sqlalchemy import text as _text

        a_keys = [(r["grain"], r["entity_key"], r["direction"], r["period_id"], r["method"])
                  for r in anomalies]
        f_keys = [(r["grain"], r["entity_key"], r["direction"], r["period_label"], r["model"])
                  for r in forecasts]

        # Insights first: they reference the anomalies about to go.
        s.execute(_text("""
            DELETE FROM insight WHERE anomaly_id IN (
                SELECT anomaly_id FROM anomaly
                WHERE (grain::text, entity_key, direction::text, period_id, method)
                      NOT IN (SELECT * FROM unnest(
                          CAST(:g AS text[]), CAST(:e AS text[]), CAST(:d AS text[]),
                          CAST(:p AS int[]), CAST(:m AS text[])))
            )
        """), {"g": [k[0] for k in a_keys], "e": [k[1] for k in a_keys],
               "d": [k[2] for k in a_keys], "p": [k[3] for k in a_keys],
               "m": [k[4] for k in a_keys]} if a_keys else {"g": [], "e": [], "d": [], "p": [], "m": []})

        if a_keys:
            s.execute(_text("""
                DELETE FROM anomaly
                WHERE (grain::text, entity_key, direction::text, period_id, method)
                      NOT IN (SELECT * FROM unnest(
                          CAST(:g AS text[]), CAST(:e AS text[]), CAST(:d AS text[]),
                          CAST(:p AS int[]), CAST(:m AS text[])))
            """), {"g": [k[0] for k in a_keys], "e": [k[1] for k in a_keys],
                   "d": [k[2] for k in a_keys], "p": [k[3] for k in a_keys],
                   "m": [k[4] for k in a_keys]})
        else:
            s.execute(_text("DELETE FROM insight"))
            s.execute(_text("DELETE FROM anomaly"))

        if f_keys:
            s.execute(_text("""
                DELETE FROM forecast
                WHERE (grain::text, entity_key, direction::text, period_label, model)
                      NOT IN (SELECT * FROM unnest(
                          CAST(:g AS text[]), CAST(:e AS text[]), CAST(:d AS text[]),
                          CAST(:p AS text[]), CAST(:m AS text[])))
            """), {"g": [k[0] for k in f_keys], "e": [k[1] for k in f_keys],
                   "d": [k[2] for k in f_keys], "p": [k[3] for k in f_keys],
                   "m": [k[4] for k in f_keys]})
        else:
            s.execute(_text("DELETE FROM forecast"))

    # ---------------------------------------------------------- policy --

    def _plan(self, history: list[ToolCall], context: dict[str, Any]) -> Decision:
        done = [c.tool for c in history if c.ok]
        if "load_series" not in done:
            return Decision("load_series", {}, "need the facts")
        if not context.get("series"):
            return Decision(None, {}, "no series long enough to analyse")
        if "compute_trends" not in done:
            return Decision("compute_trends", {}, "growth and share first")
        if "detect_anomalies" not in done:
            return Decision("detect_anomalies", {}, "flag unusual movements")
        if "build_forecasts" not in done:
            return Decision("build_forecasts", {}, "project forward")
        if "persist" not in done:
            return Decision("persist", {}, "store results for citation")
        return Decision(None, {}, "analytics complete")

    def is_goal_met(self, context: dict[str, Any]) -> bool:
        return "written" in context

    def summarise(self, context: dict[str, Any]) -> str:
        w = context.get("written") or {}
        return (
            f"{len(context.get('series', []))} series -> "
            f"{w.get('trend', 0)} trends, {w.get('anomaly', 0)} anomalies, "
            f"{w.get('forecast', 0)} forecasts"
        )


def main() -> None:
    agent = AnalyticsAgent()
    run = agent.run("compute trends, anomalies and forecasts over the warehouse")
    print("\n" + "=" * 58)
    print("ANALYTICS")
    print("=" * 58)
    print(f"  {run.result_summary}")
    print(f"  steps traced: {run.steps}   succeeded: {run.succeeded}")
    with Session(agent.engine) as s:
        for label, sql in [
            ("trend rows", "SELECT count(*) FROM trend"),
            ("anomaly rows", "SELECT count(*) FROM anomaly"),
            ("forecast rows", "SELECT count(*) FROM forecast"),
            ("HIGH severity", "SELECT count(*) FROM anomaly WHERE severity='HIGH'"),
        ]:
            print(f"  {label:16} {s.execute(text(sql)).scalar_one()}")
    print("=" * 58)


if __name__ == "__main__":
    main()
