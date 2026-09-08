"""Natural-language questions, answered from stored rows only.

This is where the project's central rule has to actually hold, so it is
enforced by construction rather than by instruction:

* The question is classified into an intent, and each intent maps to a
  fixed QuerySpec built from registered metrics. A question never
  becomes free-form SQL.
* The answer sentence is assembled from the returned rows. Numbers are
  formatted, never composed - there is no step in which a figure could
  be invented.
* Every figure quoted is checked back against the rows before the answer
  is returned. An answer that fails that check is not sent.

A language model, where one is configured, may only choose the intent
and pull entity names out of the question. It never sees a number and
never writes one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from services.common.logging import get_logger
from services.semantic.compiler import QuerySpec
from services.semantic.executor import run

log = get_logger(__name__)


@dataclass
class Intent:
    name: str
    description: str
    patterns: list[str]


INTENTS = [
    Intent("airport_ranking",
           "Which airports moved the most cargo, or grew fastest.",
           [r"\b(top|biggest|largest|busiest|rank|ranking)\b.*\bairport",
            r"\bairport.*\b(top|rank|ranking|growth|grew|growing)\b",
            r"\bwhich airports?\b"]),
    Intent("airline_ranking",
           "Which airlines carried the most cargo.",
           [r"\b(top|biggest|largest|rank|ranking)\b.*\b(airline|carrier)",
            r"\b(airline|carrier)s?\b.*\b(top|rank|most|share)\b",
            r"\bwhich (airline|carrier)s?\b"]),
    Intent("anomaly_list",
           "What unusual cargo movements have been detected.",
           [r"\banomal", r"\bunusual\b", r"\bspike", r"\bdrop(ped)?\b",
            r"\bwhat.*\bwrong\b", r"\balert"]),
    Intent("forecast_list",
           "What cargo volumes are projected.",
           [r"\bforecast", r"\bpredict", r"\bproject(ion|ed)?\b",
            r"\bnext (quarter|month|year)\b", r"\bexpect"]),
    Intent("airport_trend",
           "How one airport's cargo has moved over time.",
           [r"\btrend\b", r"\bover time\b", r"\bhistory\b",
            r"\bhow (has|did)\b.*\bchange"]),
    Intent("source_list",
           "Where the data comes from.",
           [r"\bsource", r"\bwhere.*\bdata.*from\b", r"\bpublisher"]),
]

_IATA = re.compile(r"\b([A-Z]{3})\b")
_PERIOD = re.compile(r"\b(20\d{2})[-/](0[1-9]|1[0-2])\b")
_DIRECTION = {
    "international": "INTERNATIONAL",
    "domestic": "DOMESTIC",
    "overall": "TOTAL",
    "total": "TOTAL",
}


def classify(question: str) -> Intent | None:
    q = question.lower()
    for intent in INTENTS:
        if any(re.search(p, q) for p in intent.patterns):
            return intent
    return None


def _direction(question: str) -> str:
    q = question.lower()
    for word, value in _DIRECTION.items():
        if word in q:
            return value
    return "TOTAL"


def _wants_growth(question: str) -> bool:
    return bool(re.search(r"\b(grow|growth|grew|fastest|increase|rising)\b",
                          question.lower()))


def answer(session: Session, question: str) -> dict[str, Any]:
    """Answer a question, or decline. Never guess."""
    intent = classify(question)
    if intent is None:
        return {
            "answer": (
                "I can answer questions about airport rankings, airline cargo "
                "share, detected anomalies, forecasts, and where the data comes "
                "from. I could not tell which of those you meant, so I have not "
                "guessed."
            ),
            "intent": "unknown",
            "understood_as": "no recognised intent",
            "rows": [], "chart": None, "citations": [], "grounded": True,
        }

    direction = _direction(question)
    handler = {
        "airport_ranking": _airport_ranking,
        "airline_ranking": _airline_ranking,
        "anomaly_list": _anomalies,
        "forecast_list": _forecasts,
        "airport_trend": _airport_trend,
        "source_list": _sources,
    }[intent.name]

    payload = handler(session, question, direction)
    payload["intent"] = intent.name

    # The invariant, checked rather than trusted: every number in the
    # sentence must appear in the rows behind it.
    payload["grounded"] = _verify_grounded(payload["answer"], payload["rows"])
    if not payload["grounded"]:
        log.error(f"ungrounded answer suppressed for question: {question!r}")
        payload["answer"] = (
            "I found data for this question but could not verify every figure "
            "against a stored row, so I have not reported it."
        )
        payload["rows"] = []
    return payload


# ------------------------------------------------------------- handlers --

def _airport_ranking(session: Session, question: str, direction: str) -> dict:
    by_growth = _wants_growth(question)
    order = "growth_yoy_pct" if by_growth else "tonnage_mt"
    period = _latest_period(session, "AIRPORT")
    filters: dict[str, Any] = {
        "grain": "AIRPORT", "direction": direction, "period": period,
    }
    if by_growth:
        # Percentage growth is meaningless at trivial volumes: a 4-tonne
        # airport posting 486% would otherwise top every growth ranking.
        filters["min_tonnage_kg"] = 100_000.0
    result = run(session, QuerySpec(
        metrics=["tonnage_mt", "growth_yoy_pct"],
        dimensions=["airport_iata", "airport_name"],
        filters=filters, order_by=order, limit=5,
    ))
    rows = [r for r in result.rows if r.get(order) is not None]
    if not rows:
        return _empty(f"no airport data for {period}", result)

    lead = rows[0]
    if by_growth:
        sentence = (
            f"For {period} ({direction.lower()}), {lead['airport_name']} "
            f"({lead['airport_iata']}) grew fastest at {lead['growth_yoy_pct']:.1f}% "
            f"year on year, moving {lead['tonnage_mt']:,.1f} MT."
        )
    else:
        sentence = (
            f"For {period} ({direction.lower()}), {lead['airport_name']} "
            f"({lead['airport_iata']}) handled the most cargo at "
            f"{lead['tonnage_mt']:,.1f} MT."
        )
    others = ", ".join(
        f"{r['airport_iata']} {r['tonnage_mt']:,.1f} MT" for r in rows[1:4]
    )
    if others:
        sentence += f" Next were {others}."

    return {
        "answer": sentence,
        "understood_as": (
            f"top airports by {'year-on-year growth' if by_growth else 'tonnage'}, "
            f"{direction.lower()}, {period}"
        ),
        "rows": rows,
        "chart": {"type": "bar", "x": "airport_iata", "y": order},
        "citations": [c.to_dict() for c in result.citations],
    }


def _airline_ranking(session: Session, question: str, direction: str) -> dict:
    result = run(session, QuerySpec(
        metrics=["tonnage_mt"],
        dimensions=["airline_name"],
        filters={"grain": "AIRLINE", "direction": direction,
                 "exclude_aggregate_airlines": True},
        order_by="tonnage_mt", limit=5,
    ))
    rows = result.rows
    if not rows:
        return _empty("no airline data held", result)
    lead = rows[0]
    sentence = (
        f"Across every period held ({direction.lower()}), {lead['airline_name']} "
        f"carried the most cargo at {lead['tonnage_mt']:,.1f} MT. "
        "Industry totals such as 'All Scheduled Indian Airlines' are excluded, "
        "because adding them to individual carriers double-counts the market."
    )
    return {
        "answer": sentence,
        "understood_as": f"top carriers by tonnage, {direction.lower()}, all periods",
        "rows": rows,
        "chart": {"type": "bar", "x": "airline_name", "y": "tonnage_mt"},
        "citations": [c.to_dict() for c in result.citations],
    }


def _anomalies(session: Session, question: str, direction: str) -> dict:
    from sqlalchemy import text

    rows = [dict(r) for r in session.execute(text("""
        SELECT entity_key, COALESCE(airport_name, airline_name) AS entity_name,
               grain, direction, period,
               ROUND((observed_kg/1000)::numeric,1) AS observed_mt,
               ROUND((expected_kg/1000)::numeric,1) AS expected_mt,
               ROUND(deviation_pct::numeric,1) AS deviation_pct, severity
        FROM v_anomaly ORDER BY abs(deviation_pct) DESC NULLS LAST LIMIT 5
    """)).mappings().all()]
    if not rows:
        return {"answer": "No anomalies are currently flagged.",
                "understood_as": "detected anomalies", "rows": [],
                "chart": None, "citations": []}
    top = rows[0]
    sentence = (
        f"The largest flagged movement is {top['entity_name'] or top['entity_key']} "
        f"in {top['period']} ({top['direction'].lower()}): {top['observed_mt']:,.1f} MT "
        f"against an expected {top['expected_mt']:,.1f} MT, "
        f"a {top['deviation_pct']:,.1f}% deviation ({top['severity'].lower()} severity). "
        f"{len(rows)} of the most deviant are listed."
    )
    return {"answer": sentence, "understood_as": "detected anomalies, most deviant first",
            "rows": rows, "chart": None, "citations": []}


def _forecasts(session: Session, question: str, direction: str) -> dict:
    from sqlalchemy import text

    rows = [dict(r) for r in session.execute(text("""
        SELECT entity_key, COALESCE(airport_name, airline_name) AS entity_name,
               grain, direction, period, horizon,
               ROUND((predicted_kg/1000)::numeric,1) AS predicted_mt,
               ROUND((lower_kg/1000)::numeric,1) AS lower_mt,
               ROUND((upper_kg/1000)::numeric,1) AS upper_mt,
               model, ROUND(backtest_mape::numeric,1) AS backtest_mape_pct
        FROM v_forecast WHERE horizon = 1
        ORDER BY predicted_kg DESC LIMIT 5
    """)).mappings().all()]
    if not rows:
        return {"answer": "No forecasts have been produced yet.",
                "understood_as": "forecasts", "rows": [], "chart": None,
                "citations": []}
    top = rows[0]
    sentence = (
        f"The largest projection is {top['entity_name'] or top['entity_key']} for "
        f"{top['period']}: {top['predicted_mt']:,.1f} MT, with an 80% interval of "
        f"{top['lower_mt']:,.1f} to {top['upper_mt']:,.1f} MT "
        f"({top['model']}, backtest error {top['backtest_mape_pct']:,.1f}%). "
        "Intervals are reported because these series are short."
    )
    return {"answer": sentence, "understood_as": "one-step forecasts, largest first",
            "rows": rows, "chart": None, "citations": []}


def _airport_trend(session: Session, question: str, direction: str) -> dict:
    iata = _find_airport_code(session, question)
    if not iata:
        return {
            "answer": "Name an airport by its three-letter code, for example DEL.",
            "understood_as": "airport trend, but no airport identified",
            "rows": [], "chart": None, "citations": [],
        }
    result = run(session, QuerySpec(
        metrics=["tonnage_mt", "growth_yoy_pct"],
        dimensions=["period", "airport_iata"],
        filters={"grain": "AIRPORT", "direction": direction, "airport_iata": iata},
        order_by="period", descending=False, limit=200,
    ))
    if not result.rows:
        return _empty(f"no data held for {iata}", result)
    first, last = result.rows[0], result.rows[-1]
    sentence = (
        f"{iata} ({direction.lower()}) moved {last['tonnage_mt']:,.1f} MT in "
        f"{last['period']}, against {first['tonnage_mt']:,.1f} MT in "
        f"{first['period']}, across {len(result.rows)} periods held."
    )
    return {
        "answer": sentence,
        "understood_as": f"{iata} cargo over time, {direction.lower()}",
        "rows": result.rows,
        "chart": {"type": "line", "x": "period", "y": "tonnage_mt"},
        "citations": [c.to_dict() for c in result.citations],
    }


def _sources(session: Session, question: str, direction: str) -> dict:
    from sqlalchemy import text

    rows = [dict(r) for r in session.execute(text("""
        SELECT publisher, count(*) AS documents, sum(fact_count) AS facts
        FROM v_source GROUP BY publisher ORDER BY facts DESC NULLS LAST
    """)).mappings().all()]
    listed = ", ".join(f"{r['publisher']} ({r['documents']} documents)" for r in rows)
    return {
        "answer": f"The data comes from {listed}. Every stored figure carries the "
                  f"document it was parsed from.",
        "understood_as": "source publishers and document counts",
        "rows": rows, "chart": None, "citations": [],
    }


# -------------------------------------------------------------- helpers --

def _empty(reason: str, result) -> dict:
    return {
        "answer": f"I have no data to answer that: {reason}.",
        "understood_as": reason, "rows": [], "chart": None,
        "citations": [c.to_dict() for c in result.citations],
    }


def _find_airport_code(session: Session, question: str) -> str | None:
    """Pull an airport code out of a question, and check it is real.

    Uppercasing the question first made every three-letter word a
    candidate, so "How has DEL changed" resolved to the airport "HOW".
    Codes are matched as written and then confirmed against the airport
    dimension, so an unknown code is refused rather than queried.
    """
    from sqlalchemy import text

    candidates = _IATA.findall(question)          # as written, not upper-cased
    if not candidates:
        candidates = [w for w in re.findall(r"\b[A-Za-z]{3}\b", question)]
    for code in candidates:
        hit = session.execute(
            text("SELECT 1 FROM dim_airport WHERE iata_code = :c LIMIT 1"),
            {"c": code.upper()},
        ).first()
        if hit:
            return code.upper()
    return None


def _latest_period(session: Session, grain: str) -> str:
    from sqlalchemy import text

    row = session.execute(text("""
        SELECT period FROM v_cargo_fact WHERE grain = :grain
        ORDER BY sort_key DESC LIMIT 1
    """), {"grain": grain}).first()
    return row[0] if row else "unknown"


# Period labels look like numbers and are not measurements. "2026-07"
# was being read as the figure -7, which then failed to appear in the
# rows and marked a perfectly grounded answer as unverifiable.
_PERIOD_LABEL = re.compile(r"\b\d{4}-(?:\d{2}|FY|A)\b")
_NUMBER_IN_TEXT = re.compile(r"-?\d[\d,]*\.?\d*")


def _verify_grounded(answer_text: str, rows: list[dict]) -> bool:
    """Every figure in the sentence must appear in the rows behind it.

    This is the check that turns "the model never computes a number"
    from a claim into something the code enforces. Counts and years are
    exempt: they describe the answer rather than assert a measurement.
    """
    if not rows:
        return True

    from decimal import Decimal

    stored: set[str] = set()
    for row in rows:
        for value in row.values():
            # Decimal is neither int nor float, so a plain isinstance
            # check against those two silently skipped every value that
            # SQL had rounded - which is most of them.
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                stored.add(f"{float(value):.1f}")
                stored.add(f"{float(value):.0f}")

    scrubbed = _PERIOD_LABEL.sub(" ", answer_text)
    for token in _NUMBER_IN_TEXT.findall(scrubbed):
        cleaned = token.replace(",", "")
        try:
            number = float(cleaned)
        except ValueError:
            continue
        if number.is_integer() and 0 <= number <= max(2100, len(rows) * 10):
            continue                      # a count, a year, a row total
        if f"{number:.1f}" in stored or f"{number:.0f}" in stored:
            continue
        log.warning(f"ungrounded figure {number} in answer")
        return False
    return True
