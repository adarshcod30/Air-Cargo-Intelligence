"""Insight agent: explains a detected anomaly in plain language.

Agent 6 of 6, and the only one that writes prose. Everything the
narrative says about a number comes from rows this agent is handed; it
never queries, never calculates, and never sees the warehouse.

The grounding rule is enforced the same way it is on the chat path, and
for the same reason: an explanation that quotes a figure absent from its
own evidence is not published. That check runs whichever policy produced
the text, so a model being configured changes the wording and never the
guarantee.

Without a model the agent still works. It composes the explanation from
a template over the same evidence, which is what keeps the pipeline
runnable in CI and on a laptop with no API key.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from services.agents.base import Agent, Decision
from services.agents.policy import default_policy
from services.common.bedrock import BedrockUnavailable, get_client
from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.common.models import ToolCall
from services.warehouse.loader import get_engine
from services.warehouse.schema import Insight

log = get_logger(__name__)

# How many anomalies to explain in one run. The alert feed is meant to be
# read, so explaining everything defeats the point of ranking them.
DEFAULT_LIMIT = 25


class InsightAgent(Agent):
    name = "insight"

    def __init__(self, engine=None, limit: int = DEFAULT_LIMIT) -> None:
        super().__init__(policy=default_policy(self._plan))
        self.engine = engine or get_engine()
        self.limit = limit
        self._register_tools()

    # ----------------------------------------------------------- tools --

    def _register_tools(self) -> None:
        @self.tool("load_anomalies", "Read the most deviant anomalies awaiting explanation.")
        def load_anomalies() -> int:
            with Session(self.engine) as s:
                rows = s.execute(text("""
                    SELECT a.anomaly_id, a.grain::text AS grain, a.entity_key,
                           COALESCE(ap.airport_name, al.airline_name) AS entity_name,
                           a.direction::text AS direction, p.period_label AS period,
                           a.observed_kg, a.expected_kg, a.deviation_pct,
                           a.z_score, a.method, a.severity
                    FROM anomaly a
                    JOIN dim_period p ON p.period_id = a.period_id
                    LEFT JOIN dim_airport ap ON ap.iata_code = a.entity_key
                    LEFT JOIN dim_airline al ON al.airline_name = a.entity_key
                    WHERE NOT EXISTS (
                        SELECT 1 FROM insight i WHERE i.anomaly_id = a.anomaly_id
                    )
                    ORDER BY abs(a.deviation_pct) DESC NULLS LAST
                    LIMIT :lim
                """), {"lim": self.limit}).mappings().all()
            self.context["anomalies"] = [dict(r) for r in rows]
            return len(rows)

        @self.tool("gather_evidence", "Collect the surrounding series for each anomaly.")
        def gather_evidence() -> int:
            """Context an explanation may draw on, and nothing beyond it.

            The narrative is allowed to mention the neighbouring periods
            and the source documents. Fetching them here, rather than
            letting the writer ask, is what bounds what it can assert.
            """
            out = []
            with Session(self.engine) as s:
                for a in self.context["anomalies"]:
                    series = s.execute(text("""
                        SELECT period, ROUND((tonnage_kg/1000)::numeric, 1) AS mt
                        FROM v_trend
                        WHERE grain = :g AND entity_key = :e AND direction = :d
                        ORDER BY sort_key
                    """), {"g": a["grain"], "e": a["entity_key"],
                           "d": a["direction"]}).mappings().all()
                    sources = s.execute(text("""
                        SELECT DISTINCT source_document_id, publisher, source_url
                        FROM v_cargo_fact
                        WHERE grain = :g AND period = :p
                          AND (airport_iata = :e OR airline_name = :e)
                        LIMIT 5
                    """), {"g": a["grain"], "p": a["period"],
                           "e": a["entity_key"]}).mappings().all()
                    out.append({
                        "anomaly": a,
                        "series": [dict(r) for r in series],
                        "sources": [dict(r) for r in sources],
                    })
            self.context["evidence"] = out
            return len(out)

        @self.tool("write_narratives", "Explain each anomaly from its evidence.")
        def write_narratives() -> dict:
            written, rejected = [], 0
            for item in self.context["evidence"]:
                text_out = self._narrate(item)
                if not _is_grounded(text_out, item):
                    # An explanation that quotes a figure absent from its
                    # own evidence is not published, whichever policy
                    # produced it.
                    rejected += 1
                    log.warning(
                        f"ungrounded narrative rejected for anomaly "
                        f"{item['anomaly']['anomaly_id']}"
                    )
                    continue
                written.append({
                    "anomaly_id": item["anomaly"]["anomaly_id"],
                    "headline": _headline(item["anomaly"]),
                    "narrative": text_out,
                    "citations": [s["source_url"] for s in item["sources"]],
                })
            self.context["narratives"] = written
            self.context["rejected"] = rejected
            return {"written": len(written), "rejected": rejected}

        @self.tool("persist", "Store the explanations against their anomalies.")
        def persist() -> int:
            rows = self.context.get("narratives", [])
            with Session(self.engine) as s:
                for r in rows:
                    s.execute(insert(Insight).values(
                        anomaly_id=r["anomaly_id"],
                        headline=r["headline"][:400],
                        narrative=r["narrative"],
                        citations=json.dumps(r["citations"]),
                    ))
                s.commit()
            self.context["persisted"] = len(rows)
            return len(rows)

    # -------------------------------------------------------- narration --

    def _narrate(self, item: dict) -> str:
        """Compose the explanation. Prefers a model, falls back to a template.

        Either way the figures come from `item`, so the two paths differ
        in phrasing rather than in what they are able to claim.
        """
        if SETTINGS.llm_available:
            drafted = self._narrate_with_model(item)
            if drafted:
                return drafted
        return _narrate_from_template(item)

    def _narrate_with_model(self, item: dict) -> str | None:
        """Narrate with a managed model, or return None to use the template.

        This is the one place a model writes prose that a user reads, and it
        still writes no number: every figure is handed to it in the prompt,
        and _is_grounded() rejects the output if a figure appears that does
        not resolve to a stored row. The rejection count is reported, so
        "0 rejected" is a measurement rather than an assumption.
        """
        a = item["anomaly"]
        # The model is shown tonnes and checked against tonnes. It used to be
        # shown kilograms and checked against the tonne conversion, so a
        # narrative that quoted its evidence exactly was rejected as
        # ungrounded - all 25 of them. Widening the check to accept either
        # unit would have hidden the mismatch and let a genuine unit error
        # through; presenting one unit removes the disagreement instead.
        evidence = {
            "airport_or_airline": a.get("entity_name") or a.get("entity_key"),
            "period": a.get("period"),
            "direction": a.get("direction"),
            "observed_mt": None if a.get("observed_kg") is None
            else round(float(a["observed_kg"]) / 1000, 1),
            "expected_mt": None if a.get("expected_kg") is None
            else round(float(a["expected_kg"]) / 1000, 1),
            "deviation_pct": None if a.get("deviation_pct") is None
            else round(float(a["deviation_pct"]), 1),
            "severity": a.get("severity"),
            "units": "all tonnages are metric tonnes (MT)",
        }
        series_mt = [
            {"period": r["period"], "mt": round(float(r["mt"]), 1)} for r in item["series"]
        ]
        prompt = (
            "Explain this detected air-cargo anomaly in two or three plain "
            "sentences for a logistics analyst.\n\n"
            "Rules you must follow:\n"
            "- Use ONLY the figures given below. Do not introduce any other "
            "number, percentage or date.\n"
            "- Do not speculate about causes you cannot see in the data. If "
            "the cause is not evident, say the movement is unexplained.\n"
            "- Do not repeat the figures more precisely than they are given.\n\n"
            "- Every tonnage below is already in metric tonnes. Quote them "
            "as given; do not convert to kilograms.\n\n"
            f"ANOMALY: {json.dumps(_jsonable(evidence), indent=2)}\n\n"
            f"SURROUNDING SERIES: {json.dumps(_jsonable(series_mt), indent=2)}\n"
        )
        try:
            return get_client().converse(
                prompt,
                system=(
                    "You are a logistics analyst writing a short factual note. "
                    "You never introduce a figure that was not given to you."
                ),
                max_tokens=220,
                temperature=0.2,
            ).strip()
        except BedrockUnavailable as exc:
            log.warning(f"narration model unavailable ({exc}); using template")
            return None
        except Exception as exc:
            log.warning(f"narration failed ({type(exc).__name__}); using template")
            return None

    # ---------------------------------------------------------- policy --

    def _plan(self, history: list[ToolCall], context: dict[str, Any]) -> Decision:
        done = [c.tool for c in history if c.ok]
        if "load_anomalies" not in done:
            return Decision("load_anomalies", {}, "find anomalies without an explanation")
        if not context.get("anomalies"):
            return Decision(None, {}, "every anomaly already has an explanation")
        if "gather_evidence" not in done:
            return Decision("gather_evidence", {}, "collect what an explanation may draw on")
        if "write_narratives" not in done:
            return Decision("write_narratives", {}, "explain each one")
        if "persist" not in done:
            return Decision("persist", {}, "store them against their anomalies")
        return Decision(None, {}, "insights complete")

    def is_goal_met(self, context: dict[str, Any]) -> bool:
        """Met when the explanations are stored, not when they are composed.

        This previously returned True as soon as `narratives` appeared in
        the context, which `write_narratives` sets. The base loop checks
        this after every tool call and breaks when it holds, so `persist`
        - the next step the plan schedules - was never reached, and the run
        reported "25 insight(s) written" while the table stayed empty.

        A goal predicate that is satisfied one step before the durable
        effect will always report success and never produce it.
        """
        if context.get("anomalies") == []:
            return True
        return "persisted" in context

    def summarise(self, context: dict[str, Any]) -> str:
        n = context.get("persisted")
        r = context.get("rejected", 0)
        if n is None:
            drafted = len(context.get("narratives", []))
            return f"{drafted} insight(s) drafted but NOT stored, {r} rejected as ungrounded"
        return f"{n} insight(s) stored, {r} rejected as ungrounded"


# ------------------------------------------------------------- helpers --

def _jsonable(obj):
    from decimal import Decimal
    if isinstance(obj, list):
        return [_jsonable(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


def _headline(a: dict) -> str:
    name = a.get("entity_name") or a["entity_key"]
    direction = a["direction"].lower()
    move = "rose" if (a.get("deviation_pct") or 0) > 0 else "fell"
    return f"{name} {direction} cargo {move} sharply in {a['period']}"


def _narrate_from_template(item: dict) -> str:
    """The deterministic explanation, used when no model is configured.

    Deliberately says what the data shows and stops. Naming a cause the
    data cannot support would be the exact failure the whole grounding
    design exists to prevent.
    """
    a = item["anomaly"]
    name = a.get("entity_name") or a["entity_key"]
    observed = float(a["observed_kg"]) / 1000
    expected = float(a["expected_kg"]) / 1000 if a.get("expected_kg") else None
    dev = float(a["deviation_pct"]) if a.get("deviation_pct") is not None else None
    series = item["series"]

    parts = [
        f"{name} handled {observed:,.1f} MT of {a['direction'].lower()} cargo "
        f"in {a['period']}."
    ]
    if expected is not None and dev is not None:
        parts.append(
            f"That is {abs(dev):,.1f}% {'above' if dev > 0 else 'below'} the "
            f"{expected:,.1f} MT expected from its own recent history, "
            f"flagged by {a['method']} at {a['severity'].lower()} severity."
        )
    if len(series) >= 3:
        parts.append(
            f"The series runs across {len(series)} periods, from "
            f"{series[0]['period']} to {series[-1]['period']}."
        )
    parts.append(
        "The published data does not record a reason, so the cause is "
        "unexplained here and needs checking against operational reporting."
    )
    return " ".join(parts)


def _is_grounded(narrative: str, item: dict) -> bool:
    """Every figure in the prose must appear in the evidence behind it."""
    import re

    a = item["anomaly"]
    allowed: set[str] = set()
    for value in (a.get("observed_kg"), a.get("expected_kg")):
        if value is not None:
            allowed.add(f"{float(value) / 1000:.1f}")
            allowed.add(f"{float(value) / 1000:.0f}")
    for value in (a.get("deviation_pct"), a.get("z_score")):
        if value is not None:
            allowed.add(f"{abs(float(value)):.1f}")
            allowed.add(f"{abs(float(value)):.0f}")
    for row in item["series"]:
        allowed.add(f"{float(row['mt']):.1f}")
        allowed.add(f"{float(row['mt']):.0f}")
    allowed.add(f"{len(item['series'])}")

    scrubbed = re.sub(r"\b\d{4}-(?:\d{2}|FY|A)\b", " ", narrative)
    for token in re.findall(r"\d[\d,]*\.?\d*", scrubbed):
        try:
            number = float(token.replace(",", ""))
        except ValueError:
            continue
        if number.is_integer() and 0 <= number <= 2100:
            continue                       # a count or a year
        if f"{number:.1f}" in allowed or f"{number:.0f}" in allowed:
            continue
        log.warning(f"ungrounded figure {number} in narrative")
        return False
    return True


def main() -> None:
    agent = InsightAgent()
    run = agent.run("explain the most deviant anomalies from their evidence")
    print("\n" + "=" * 58)
    print("INSIGHTS")
    print("=" * 58)
    print(f"  {run.result_summary}")
    print(f"  steps traced: {run.steps}   succeeded: {run.succeeded}")
    with Session(agent.engine) as s:
        total = s.execute(text("SELECT count(*) FROM insight")).scalar_one()
        print(f"  insight rows: {total}")
        for r in s.execute(text(
            "SELECT headline, narrative FROM insight ORDER BY insight_id DESC LIMIT 3"
        )).mappings():
            print(f"\n  {r['headline']}")
            print(f"    {r['narrative'][:250]}")
    print("=" * 58)


if __name__ == "__main__":
    main()
