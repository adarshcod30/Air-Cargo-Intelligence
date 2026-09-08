"""Persisting agent decision traces.

The agents already produced a complete `AgentRun` for every invocation and
appended it to a JSONL file. That was enough to audit a run by hand and not
nearly enough to build a product on: a file on the ingest machine cannot be
queried by the API, joined against the facts a run produced, or compared
across policies.

This module moves the trace into the warehouse, where it becomes a first
class object - and adds the derived columns the console needs (elapsed
time, token spend, how many decisions fell back off the model) so the read
path does not have to recompute them per request.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from services.common.logging import get_logger, redact
from services.common.models import AgentRun
from services.warehouse.schema import AgentRunRow, AgentStepRow

log = get_logger(__name__)


def new_trace_id() -> str:
    """One id per pipeline invocation, shared by every agent in it."""
    return uuid.uuid4().hex[:16]


def _parse_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v:
        return datetime.fromisoformat(v)
    return None


def _row_from_dict(d: dict, trace_id: str) -> tuple[AgentRunRow, list[AgentStepRow]]:
    started = _parse_dt(d.get("started_at"))
    finished = _parse_dt(d.get("finished_at"))
    elapsed = int((finished - started).total_seconds() * 1000) if started and finished else 0

    calls = d.get("calls") or []
    # A step whose policy is a fallback label did not come from the model.
    # Counting them here rather than in the API keeps the console cheap and
    # makes the number auditable in SQL.
    fallbacks = sum(1 for c in calls if str(c.get("policy", "")).startswith("heuristic(fallback"))

    run = AgentRunRow(
        trace_id=trace_id,
        agent=d.get("agent", "unknown"),
        goal=redact(d.get("goal", "")),
        policy=d.get("policy", ""),
        succeeded=bool(d.get("succeeded")),
        steps=int(d.get("steps") or len(calls)),
        result_summary=redact(d.get("result_summary", "") or ""),
        started_at=started or datetime.now().astimezone(),
        finished_at=finished,
        elapsed_ms=elapsed,
        input_tokens=int((d.get("usage") or {}).get("input_tokens", 0)),
        output_tokens=int((d.get("usage") or {}).get("output_tokens", 0)),
        fallback_steps=fallbacks,
    )
    steps = [
        AgentStepRow(
            seq=i,
            tool=str(c.get("tool") or "")[:60],
            args=json.dumps(c.get("args") or {}, default=str),
            ok=bool(c.get("ok")),
            observation=redact(str(c.get("observation") or ""))[:8000],
            reasoning=redact(str(c.get("reasoning") or ""))[:2000],
            policy=str(c.get("policy") or "")[:80],
            elapsed_ms=int(c.get("elapsed_ms") or 0),
        )
        for i, c in enumerate(calls)
    ]
    return run, steps


def persist_run(
    session: Session, run: AgentRun, trace_id: str, usage: dict[str, int] | None = None
) -> int:
    """Write one completed trace. Returns the new agent_run_id."""
    payload = run.to_dict()
    if usage:
        payload["usage"] = usage
    row, steps = _row_from_dict(payload, trace_id)
    row.steps_rel = steps
    session.add(row)
    session.flush()
    return int(row.agent_run_id)


def persist_runs(
    session: Session, runs: Iterable[AgentRun], trace_id: str,
    usage: dict[str, int] | None = None,
) -> int:
    n = 0
    for r in runs:
        persist_run(session, r, trace_id, usage=usage)
        n += 1
    session.commit()
    log.info(f"persisted {n} agent run(s) under trace {trace_id}")
    return n


def backfill_from_jsonl(session: Session, path: Path, replace: bool = True) -> int:
    """Load historical traces written before the warehouse existed.

    Each line is one run. Runs are grouped into synthetic traces by agent
    ordering: a `discovery` run starts a new trace, because the pipeline
    always begins there. That reconstructs the pipeline grouping the JSONL
    never recorded, which is the one thing lost by the file format.
    """
    if not path.exists():
        log.warning(f"no trace file at {path}")
        return 0

    if replace:
        # Backfill is idempotent: re-running it must not double the history.
        session.execute(delete(AgentStepRow))
        session.execute(delete(AgentRunRow))
        session.commit()

    trace_id = new_trace_id()
    n = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("agent") == "discovery" and n:
            trace_id = new_trace_id()
        row, steps = _row_from_dict(d, trace_id)
        row.steps_rel = steps
        session.add(row)
        n += 1
        if n % 200 == 0:
            session.commit()
    session.commit()
    log.info(f"backfilled {n} run(s) from {path.name}")
    return n


def recent_runs(session: Session, limit: int = 50) -> list[AgentRunRow]:
    return list(
        session.execute(
            select(AgentRunRow).order_by(AgentRunRow.started_at.desc()).limit(limit)
        ).scalars()
    )
