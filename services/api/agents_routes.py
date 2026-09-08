"""Read endpoints for the agent traces.

The agents have always produced a full decision trace; until now nothing
served it, so the agentic behaviour of the system was invisible to anyone
who was not reading a JSONL file on the ingest machine. These routes are
what the console renders.

Everything here reads through `v_agent_run` / `v_agent_step`, so the
serving role still holds no rights on any base table.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.api.deps import engine, get_session
from services.common.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


@router.get("/runs")
def list_runs(
    agent: str | None = Query(None, description="filter to one agent"),
    trace_id: str | None = Query(None),
    succeeded: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    """Recorded agent invocations, newest first."""
    where, params = [], {"lim": limit, "off": offset}
    if agent:
        where.append("agent = :agent")
        params["agent"] = agent
    if trace_id:
        where.append("trace_id = :trace_id")
        params["trace_id"] = trace_id
    if succeeded is not None:
        where.append("succeeded = :ok")
        params["ok"] = succeeded
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    rows = session.execute(text(f"""
        SELECT agent_run_id, trace_id, agent, goal, policy, effective_policy,
               succeeded, steps, failed_steps, fallback_steps, result_summary,
               started_at, finished_at, elapsed_ms, input_tokens, output_tokens
        FROM v_agent_run {clause}
        ORDER BY started_at DESC, agent_run_id DESC
        LIMIT :lim OFFSET :off
    """), params).mappings().all()
    total = session.execute(
        text(f"SELECT count(*) FROM v_agent_run {clause}"),
        {k: v for k, v in params.items() if k not in {"lim", "off"}},
    ).scalar()
    return {"rows": [dict(r) for r in rows], "row_count": len(rows), "total": total}


@router.get("/runs/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session)) -> dict:
    """One run with every step, in order - the replayable trace."""
    run = session.execute(text("""
        SELECT agent_run_id, trace_id, agent, goal, policy, effective_policy,
               succeeded, steps, failed_steps, fallback_steps, result_summary,
               started_at, finished_at, elapsed_ms, input_tokens, output_tokens
        FROM v_agent_run WHERE agent_run_id = :id
    """), {"id": run_id}).mappings().first()
    if run is None:
        raise HTTPException(404, f"no agent run {run_id}")

    steps = session.execute(text("""
        SELECT seq, tool, args, ok, observation, reasoning, policy, elapsed_ms
        FROM v_agent_step WHERE agent_run_id = :id ORDER BY seq
    """), {"id": run_id}).mappings().all()

    out = []
    for s in steps:
        d = dict(s)
        try:
            d["args"] = json.loads(d["args"] or "{}")
        except json.JSONDecodeError:
            d["args"] = {"_raw": d["args"]}
        out.append(d)
    return {"run": dict(run), "steps": out}


@router.get("/traces")
def list_traces(
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    """Pipeline invocations - the agents that ran together, grouped."""
    rows = session.execute(text("""
        SELECT trace_id,
               count(*)                                    AS runs,
               sum(steps)                                  AS steps,
               sum(CASE WHEN succeeded THEN 1 ELSE 0 END)  AS succeeded,
               min(started_at)                             AS started_at,
               max(finished_at)                            AS finished_at,
               sum(elapsed_ms)                             AS elapsed_ms,
               array_agg(DISTINCT agent)                   AS agents
        FROM v_agent_run
        GROUP BY trace_id ORDER BY min(started_at) DESC LIMIT :lim
    """), {"lim": limit}).mappings().all()
    return {"rows": [dict(r) for r in rows], "row_count": len(rows)}


@router.get("/stats")
def stats(session: Session = Depends(get_session)) -> dict:
    """Aggregate behaviour of the agent layer.

    `tools` is the interesting one: a tool with more calls than the runs
    that used it is a tool the agents retried, which is the observable
    signature of the loop recovering rather than executing a fixed script.
    """
    tools = session.execute(text("""
        SELECT tool, calls, ok_calls, failed_calls, avg_ms, max_ms, runs_using
        FROM v_agent_tool_stats ORDER BY calls DESC
    """)).mappings().all()

    agents = session.execute(text("""
        SELECT agent,
               count(*)                                   AS runs,
               sum(steps)                                 AS steps,
               sum(CASE WHEN succeeded THEN 1 ELSE 0 END) AS succeeded,
               round(avg(steps)::numeric, 2)              AS avg_steps,
               round(avg(elapsed_ms))                     AS avg_ms
        FROM v_agent_run GROUP BY agent ORDER BY runs DESC
    """)).mappings().all()

    policies = session.execute(text("""
        SELECT effective_policy, count(*) AS runs, sum(steps) AS steps,
               sum(CASE WHEN succeeded THEN 1 ELSE 0 END) AS succeeded
        FROM v_agent_run GROUP BY effective_policy ORDER BY runs DESC
    """)).mappings().all()

    totals = session.execute(text("""
        SELECT count(*) AS runs, sum(steps) AS steps,
               count(DISTINCT trace_id) AS traces,
               sum(input_tokens + output_tokens) AS tokens,
               sum(CASE WHEN succeeded THEN 1 ELSE 0 END) AS succeeded
        FROM v_agent_run
    """)).mappings().first()

    return {
        "totals": dict(totals or {}),
        "agents": [dict(r) for r in agents],
        "tools": [dict(r) for r in tools],
        "policies": [dict(r) for r in policies],
    }


@router.get("/stream")
async def stream(
    run_id: int | None = Query(None, description="replay this run step by step"),
    delay_ms: int = Query(420, ge=0, le=3000),
) -> StreamingResponse:
    """Server-sent events replaying a trace step by step.

    A recorded trace rather than a live tail: an ingest takes minutes and
    runs on a schedule, so streaming "live" would show an empty console
    almost always. Replaying at a readable pace shows the same decisions
    with the same data, and it is honest about being a replay - the client
    labels it as one.
    """

    def _fetch() -> tuple[dict, list[dict]]:
        with Session(engine()) as s:
            if run_id is not None:
                rid = run_id
            else:
                rid = s.execute(text(
                    "SELECT agent_run_id FROM v_agent_run "
                    "WHERE steps > 2 ORDER BY started_at DESC LIMIT 1"
                )).scalar()
            if rid is None:
                return {}, []
            run = s.execute(text("""
                SELECT agent_run_id, agent, goal, policy, effective_policy,
                       succeeded, steps, result_summary, elapsed_ms
                FROM v_agent_run WHERE agent_run_id = :id
            """), {"id": rid}).mappings().first()
            steps = s.execute(text("""
                SELECT seq, tool, args, ok, observation, reasoning, policy, elapsed_ms
                FROM v_agent_step WHERE agent_run_id = :id ORDER BY seq
            """), {"id": rid}).mappings().all()
            return dict(run or {}), [dict(x) for x in steps]

    run, steps = await asyncio.to_thread(_fetch)

    async def events():
        def sse(event: str, data: Any) -> str:
            return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

        if not run:
            yield sse("error", {"message": "no recorded runs"})
            return
        yield sse("run", run)
        for s in steps:
            await asyncio.sleep(delay_ms / 1000)
            try:
                s["args"] = json.loads(s["args"] or "{}")
            except json.JSONDecodeError:
                s["args"] = {}
            yield sse("step", s)
        await asyncio.sleep(delay_ms / 1000)
        yield sse("done", {
            "succeeded": run.get("succeeded"),
            "summary": run.get("result_summary"),
            "steps": len(steps),
        })

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------- retrieval --

search_router = APIRouter(prefix="/api/v1/search", tags=["retrieval"])


@search_router.get("")
def search_passages(
    q: str = Query(..., min_length=2, description="natural-language query"),
    top_k: int = Query(5, ge=1, le=20),
    session: Session = Depends(get_session),
) -> dict:
    """Hybrid passage search over the indexed source documents.

    Dense vectors and full text are fused on rank rather than score,
    because a cosine similarity and a ts_rank are not on comparable
    scales. Returns provenance, never a computed figure.
    """
    from services.rag.retriever import search as _search

    hits = _search(session, q, top_k=top_k)
    return hits.to_dict()


@search_router.get("/stats")
def index_stats(session: Session = Depends(get_session)) -> dict:
    """What the index actually contains, per publisher and model."""
    rows = session.execute(text("""
        SELECT publisher, count(*) AS chunks,
               count(DISTINCT source_document_id) AS documents,
               sum(token_estimate) AS tokens
        FROM v_document_chunk GROUP BY publisher ORDER BY chunks DESC
    """)).mappings().all()
    models = session.execute(text(
        "SELECT DISTINCT embed_model FROM v_document_chunk WHERE embed_model IS NOT NULL"
    )).scalars().all()
    vocab = session.execute(text("SELECT count(*) FROM v_rag_vocab")).scalar()
    return {
        "by_publisher": [dict(r) for r in rows],
        "embed_models": list(models),
        "vocabulary_terms": vocab,
    }
