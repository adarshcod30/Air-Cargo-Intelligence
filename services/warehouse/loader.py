"""Load reconciled facts from the pipeline into the warehouse.

Idempotent by construction: dimensions are upserted on their natural
keys and facts on theirs, so re-running an ingest updates rows instead of
duplicating them. That matters because the sources republish - AAI
reissues a month with corrections, and a loader that appended would
quietly double a month's tonnage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from services.common.config import SETTINGS
from services.common.logging import get_logger, redact
from services.warehouse.schema import (
    Base,
    CargoFactRow,
    DimAirline,
    DimAirport,
    DimPeriod,
    IngestRun,
    SourceDocument,
)

log = get_logger(__name__)

# Rows per INSERT. Large enough that round-trip latency stops dominating,
# small enough that one statement's parameter list stays well inside
# Postgres' 65,535 bound parameters - these rows carry twelve columns.
_BATCH = 500


def batched_upsert(
    session,
    table,
    constraint: str,
    update_cols: list[str],
    rows,
    key,
    batch: int = _BATCH,
) -> int:
    """Upsert rows a batch at a time, deduplicating each batch by natural key.

    Row-at-a-time upserts are invisible on a unix socket and pathological
    over a network: the server sits idle in transaction while each statement
    crosses the wire. Every writer in this project had the same shape, so
    the fix belongs in one place rather than three.

    Deduplicating is required, not an optimisation. Postgres rejects a
    multi-row INSERT whose ON CONFLICT would affect one row twice, and a
    recomputed period legitimately produces a repeat. Later rows win, which
    matches what a sequence of single-row upserts did implicitly.
    """
    pending: dict[tuple, dict] = {}
    written = 0

    def flush() -> None:
        nonlocal written
        if not pending:
            return
        ins = insert(table).values(list(pending.values()))
        session.execute(
            ins.on_conflict_do_update(
                constraint=constraint,
                set_={c: getattr(ins.excluded, c) for c in update_cols},
            )
        )
        written += len(pending)
        pending.clear()

    for row in rows:
        pending[key(row)] = row
        if len(pending) >= batch:
            flush()
    flush()
    return written

_PERIOD_RX = re.compile(r"^(\d{4})-(\d{2}|FY|A)$")


def get_engine(url: str | None = None):
    import os

    url = url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    return create_engine(url, future=True)


def parse_period(label: str) -> dict[str, Any]:
    """Turn a period label into sortable dimension attributes.

    Three kinds coexist and must stay distinguishable: '2026-04' is a
    calendar month, '2015-FY' an Indian fiscal year, '2023-A' a plain
    year. `sort_key` orders all three on one axis so a query does not have
    to know which kind it is looking at.
    """
    m = _PERIOD_RX.match(label or "")
    if not m:
        return {
            "period_label": label or "unknown", "period_kind": "ANNUAL",
            "calendar_year": 1900, "calendar_month": None,
            "fiscal_year_start": None, "sort_key": 0,
        }
    year, tail = int(m.group(1)), m.group(2)
    if tail == "FY":
        return {
            "period_label": label, "period_kind": "FISCAL", "calendar_year": year,
            "calendar_month": None, "fiscal_year_start": year,
            # Fiscal years start in April; sort them mid-year so they
            # interleave sensibly with monthly data.
            "sort_key": year * 100 + 6,
        }
    if tail == "A":
        return {
            "period_label": label, "period_kind": "ANNUAL", "calendar_year": year,
            "calendar_month": None, "fiscal_year_start": None,
            "sort_key": year * 100 + 12,
        }
    month = int(tail)
    return {
        "period_label": label, "period_kind": "MONTH", "calendar_year": year,
        "calendar_month": month, "fiscal_year_start": None,
        "sort_key": year * 100 + month,
    }


class WarehouseLoader:
    def __init__(self, engine=None) -> None:
        self.engine = engine or get_engine()
        self._airport_cache: dict[tuple, int] = {}
        self._airline_cache: dict[str, int] = {}
        self._period_cache: dict[str, int] = {}
        self._doc_cache: dict[str, int] = {}

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    # ------------------------------------------------------------------ #

    def _upsert_period(self, s: Session, label: str) -> int:
        if label in self._period_cache:
            return self._period_cache[label]
        attrs = parse_period(label)
        stmt = (
            insert(DimPeriod).values(**attrs)
            .on_conflict_do_update(
                index_elements=[DimPeriod.period_label],
                set_={"sort_key": attrs["sort_key"]},
            )
            .returning(DimPeriod.period_id)
        )
        pid = s.execute(stmt).scalar_one()
        self._period_cache[label] = pid
        return pid

    def _upsert_airport(self, s: Session, fact: dict) -> int | None:
        name = fact.get("airport_name") or fact.get("airport_name_raw")
        if not name:
            return None
        country = fact.get("country") or "Unknown"
        key = (name, country)
        if key in self._airport_cache:
            return self._airport_cache[key]
        values = {
            "airport_name": name, "country": country,
            "iata_code": (fact.get("airport_iata") or None),
            "icao_code": (fact.get("airport_icao") or None),
            "resolution_method": (fact.get("resolution_method") or None),
        }
        stmt = (
            insert(DimAirport).values(**values)
            .on_conflict_do_update(
                constraint="airport_name_country",
                set_={"iata_code": values["iata_code"], "icao_code": values["icao_code"]},
            )
            .returning(DimAirport.airport_id)
        )
        aid = s.execute(stmt).scalar_one()
        self._airport_cache[key] = aid
        return aid

    def _upsert_airline(self, s: Session, fact: dict) -> int | None:
        name = fact.get("airline")
        if not name:
            return None
        if name in self._airline_cache:
            return self._airline_cache[name]
        stmt = (
            insert(DimAirline)
            .values(airline_name=name, country=fact.get("country"),
                    is_aggregate=bool(fact.get("is_aggregate")))
            .on_conflict_do_update(
                index_elements=[DimAirline.airline_name],
                set_={"is_aggregate": bool(fact.get("is_aggregate"))},
            )
            .returning(DimAirline.airline_id)
        )
        aid = s.execute(stmt).scalar_one()
        self._airline_cache[name] = aid
        return aid

    def _upsert_document(self, s: Session, doc_key: str, meta: dict, run_id: int) -> int:
        if doc_key in self._doc_cache:
            return self._doc_cache[doc_key]
        stmt = (
            insert(SourceDocument)
            .values(
                doc_key=doc_key,
                publisher=meta.get("publisher") or "UNKNOWN",
                title=meta.get("title"),
                # Redacted before it ever reaches the database; a check
                # constraint rejects the row if a key slips through.
                source_url=redact(meta.get("source_url") or f"doc:{doc_key}"),
                sha256=meta.get("sha256"),
                media_type=meta.get("media_type"),
                byte_size=meta.get("byte_size"),
                raw_path=meta.get("raw_path"),
                ingest_run_id=run_id,
            )
            .on_conflict_do_update(
                index_elements=[SourceDocument.doc_key],
                set_={"ingest_run_id": run_id},
            )
            .returning(SourceDocument.source_document_id)
        )
        did = s.execute(stmt).scalar_one()
        self._doc_cache[doc_key] = did
        return did

    # ------------------------------------------------------------------ #

    @staticmethod
    def _natural_key(row: dict) -> tuple:
        """The unique constraint's columns, in order."""
        return (
            row["grain"], row["period_id"], row["airport_id"], row["airline_id"],
            row["direction"], row["publisher"], row["measure"],
        )

    @staticmethod
    def _flush(s, pending: dict) -> None:
        """Write the staged facts as one statement, then clear the buffer."""
        if not pending:
            return
        batched_upsert(
            s, CargoFactRow, "fact_natural_key",
            ["tonnage_kg", "prior_year_tonnage_kg", "reported_change_pct",
             "source_document_id", "resolution_confidence", "resolution_method"],
            list(pending.values()),
            WarehouseLoader._natural_key,
        )
        pending.clear()

    def load(self, facts_path: Path, ledger_path: Path | None = None) -> dict:
        """Load a facts JSONL file. Returns a summary."""
        facts = [json.loads(line) for line in Path(facts_path).open(encoding="utf-8")]
        ledger: dict[str, dict] = {}
        if ledger_path and Path(ledger_path).exists():
            for line in Path(ledger_path).open(encoding="utf-8"):
                d = json.loads(line)
                ledger[d.get("doc_id", "")] = d

        loaded = skipped = 0
        # Keyed by natural key so a repeat within a batch replaces rather
        # than accumulating, which is what the per-row upsert did implicitly.
        pending: dict[tuple, dict] = {}
        with Session(self.engine) as s:
            run = IngestRun(status="RUNNING")
            s.add(run)
            s.flush()

            for fact in facts:
                doc_key = fact.get("source_document_id") or "unknown"
                doc_id = self._upsert_document(
                    s, doc_key,
                    {**ledger.get(doc_key, {}), "publisher": fact.get("publisher")},
                    run.ingest_run_id,
                )
                period_id = self._upsert_period(s, fact.get("period") or "unknown")
                grain = fact.get("grain") or "AIRPORT"
                airport_id = self._upsert_airport(s, fact) if grain == "AIRPORT" else None
                airline_id = self._upsert_airline(s, fact) if grain == "AIRLINE" else None

                # The schema requires a dimension for the grain; a row that
                # cannot satisfy that is dropped here with a count rather
                # than aborting the whole load.
                if (grain == "AIRPORT" and airport_id is None) or (
                    grain == "AIRLINE" and airline_id is None
                ):
                    skipped += 1
                    continue

                row = {
                    "grain": grain, "period_id": period_id, "airport_id": airport_id,
                    "airline_id": airline_id,
                    "direction": fact.get("direction") or "TOTAL",
                    "tonnage_kg": max(0.0, float(fact.get("tonnage_kg") or 0.0)),
                    "prior_year_tonnage_kg": fact.get("prior_year_tonnage_kg"),
                    "reported_change_pct": fact.get("reported_change_pct"),
                    "publisher": fact.get("publisher") or "UNKNOWN",
                    "source_document_id": doc_id,
                    "resolution_confidence": fact.get("resolution_confidence"),
                    "resolution_method": fact.get("resolution_method"),
                    "measure": fact.get("measure") or "freight",
                }
                # Later rows supersede earlier ones for the same natural key,
                # which is the same last-wins rule the per-row upsert had.
                # Deduplicating here is not an optimisation: Postgres rejects
                # a multi-row INSERT whose ON CONFLICT would touch one row
                # twice ("cannot affect row a second time"), and a republished
                # month legitimately contains repeats.
                pending[self._natural_key(row)] = row
                loaded += 1

                if len(pending) >= _BATCH:
                    self._flush(s, pending)

            self._flush(s, pending)

            run.status = "COMPLETE"
            run.facts_loaded = loaded
            run.finished_at = func.now()
            s.commit()

        summary = {"loaded": loaded, "skipped": skipped, "input_rows": len(facts)}
        log.info(f"warehouse load: {summary}")
        return summary

    def counts(self) -> dict[str, int]:
        with Session(self.engine) as s:
            return {
                t.name: s.execute(select(func.count()).select_from(t)).scalar_one()
                for t in Base.metadata.sorted_tables
            }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Load facts into the warehouse")
    ap.add_argument("--facts", default=str(SETTINGS.processed_dir / "cargo_facts.jsonl"))
    ap.add_argument("--ledger", default=str(SETTINGS.raw_dir / "_ledger.jsonl"))
    args = ap.parse_args()

    loader = WarehouseLoader()
    summary = loader.load(Path(args.facts), Path(args.ledger))
    print("\n" + "=" * 54)
    print("WAREHOUSE LOAD")
    print("=" * 54)
    for k, v in summary.items():
        print(f"  {k:14} {v}")
    print("  --- row counts ---")
    for t, c in loader.counts().items():
        if c:
            print(f"  {t:24} {c}")
    print("=" * 54)


if __name__ == "__main__":
    main()
