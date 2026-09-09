"""Scheduled pipeline runs (SRS NFR-3: data reflected within 24 hours).

Deliberately a plain scheduler rather than a workflow engine. The
pipeline is one linear chain run once a day over a handful of public
endpoints; a distributed orchestrator would add an operational
dependency without removing any real problem here. If the schedule ever
needs backfill semantics or parallel branches, that is the point to
reach for one.

Two properties that do matter and are implemented:

* **Runs do not overlap.** These sources rate-limit, and two concurrent
  crawls is exactly how this project got throttled. A lock file makes a
  second run refuse rather than queue.
* **A failed stage does not silently pass.** Each stage records its
  outcome, and the exit code reflects the worst of them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from services.common.config import SETTINGS
from services.common.logging import get_logger

log = get_logger(__name__)

LOCK_PATH = Path(SETTINGS.processed_dir) / ".pipeline.lock"
STATE_PATH = Path(SETTINGS.processed_dir) / "pipeline_state.json"
STALE_LOCK_SECONDS = 6 * 60 * 60


@dataclass
class StageResult:
    name: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass
class RunState:
    started_at: str
    finished_at: str | None = None
    ok: bool = False
    stages: list[dict] = field(default_factory=list)


class AlreadyRunning(RuntimeError):
    pass


def _acquire_lock() -> None:
    """Refuse to start when a run is already in flight.

    A stale lock is cleared after a few hours so a crashed run cannot
    block the schedule forever - the alternative, no lock at all, is what
    caused two concurrent crawls to get this project rate limited.
    """
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < STALE_LOCK_SECONDS:
            raise AlreadyRunning(
                f"a run started {age / 60:.0f} minutes ago holds {LOCK_PATH}. "
                "Concurrent crawls get these sources to rate limit."
            )
        log.warning(f"clearing a stale lock ({age / 3600:.1f}h old)")
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(os.getpid()))


def _release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def _stage(name: str, fn) -> StageResult:
    t0 = time.perf_counter()
    try:
        detail = fn() or ""
        return StageResult(name, True, round(time.perf_counter() - t0, 1), str(detail)[:200])
    except Exception as exc:
        log.error(f"stage {name} failed: {type(exc).__name__}: {exc}")
        return StageResult(name, False, round(time.perf_counter() - t0, 1),
                           f"{type(exc).__name__}: {exc}"[:200])


def run_once(limit: int = 400, skip_ingest: bool = False) -> RunState:
    """Ingest, load, analyse, explain - in that order, each depending on the last."""
    state = RunState(started_at=datetime.now(UTC).isoformat())
    _acquire_lock()
    try:
        stages: list[StageResult] = []

        if not skip_ingest:
            def ingest():
                from services.agents.orchestrator import Pipeline
                from services.ingestion import registry as source_registry

                pipeline = Pipeline()
                facts = []
                for source in source_registry.active():
                    if source.key == "openflights_airports":
                        continue           # reference data, not cargo
                    facts.extend(pipeline.run_source(source.key, limit=limit))
                reconciled = pipeline.reconcile(facts)
                pipeline.persist(reconciled)
                return f"{len(reconciled)} facts reconciled"

            stages.append(_stage("ingest", ingest))

        def load():
            from pathlib import Path as P

            from services.warehouse.loader import WarehouseLoader
            loader = WarehouseLoader()
            summary = loader.load(P(SETTINGS.processed_dir) / "cargo_facts.jsonl",
                                  P(SETTINGS.raw_dir) / "_ledger.jsonl")
            return json.dumps(summary)

        def analytics():
            from services.agents.analytics_agent import AnalyticsAgent
            run = AnalyticsAgent().run("scheduled analytics")
            return run.result_summary

        def insights():
            from services.agents.insight_agent import InsightAgent
            run = InsightAgent().run("scheduled insight generation")
            return run.result_summary

        # Each stage depends on the one before it, so a failure stops the
        # chain rather than analysing data that never loaded.
        for name, fn in [("load", load), ("analytics", analytics), ("insights", insights)]:
            result = _stage(name, fn)
            stages.append(result)
            if not result.ok:
                break

        state.stages = [asdict(s) for s in stages]
        state.ok = all(s.ok for s in stages)
        state.finished_at = datetime.now(UTC).isoformat()
        STATE_PATH.write_text(json.dumps(asdict(state), indent=2))
        _record_state(asdict(state))
        return state
    finally:
        _release_lock()


def _record_state(payload: dict) -> None:
    """Mirror the run outcome into the warehouse.

    The file remains the record of last resort when the database is
    unreachable; the row is the one the dashboard can actually read, since
    the serving process runs somewhere the file does not exist.
    """
    try:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from services.warehouse.loader import get_engine

        with Session(get_engine()) as s:
            s.execute(
                text("""
                    INSERT INTO pipeline_state (id, payload, recorded_at)
                    VALUES (1, CAST(:p AS jsonb), now())
                    ON CONFLICT (id) DO UPDATE
                       SET payload = EXCLUDED.payload,
                           recorded_at = EXCLUDED.recorded_at
                """),
                {"p": json.dumps(payload)},
            )
            s.commit()
    except Exception as exc:
        log.warning(f"could not record pipeline state: {type(exc).__name__}: {exc}")


def last_state() -> dict | None:
    """Prefer the stored row; fall back to the local file.

    Order matters. The row is visible to whichever process is serving, and
    the file only exists on the machine that ran the pipeline.
    """
    try:
        from sqlalchemy import text
        from sqlalchemy.orm import Session

        from services.api.deps import engine

        with Session(engine()) as s:
            row = s.execute(text("SELECT payload FROM v_pipeline_state")).scalar()
        if row:
            return row if isinstance(row, dict) else json.loads(row)
    except Exception:
        pass
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the pipeline end to end")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--skip-ingest", action="store_true",
                    help="reload, analyse and explain without re-fetching")
    args = ap.parse_args()

    try:
        state = run_once(limit=args.limit, skip_ingest=args.skip_ingest)
    except AlreadyRunning as exc:
        log.error(str(exc))
        return 2

    print("\n" + "=" * 58)
    print("PIPELINE RUN")
    print("=" * 58)
    for s in state.stages:
        mark = "ok " if s["ok"] else "ERR"
        print(f"  {mark} {s['name']:12} {s['seconds']:>7.1f}s  {s['detail'][:70]}")
    print(f"  overall: {'success' if state.ok else 'FAILED'}")
    print("=" * 58)
    return 0 if state.ok else 1


if __name__ == "__main__":
    sys.exit(main())
