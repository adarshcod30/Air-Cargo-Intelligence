"""Loading per-airport passengers and movements into the warehouse.

These land in fact_operating_metric at AIRPORT grain, alongside the carrier
metrics already there. Freight stays in fact_cargo_movement: a passenger
count and a mass of cargo are different quantities and the fact table's
column is called tonnage_kg for a reason.

Each annexure is registered as a source document first, so the provenance
foreign key can be satisfied - a metric whose origin cannot be named is not
storable here, by constraint rather than convention.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.common.logging import get_logger
from services.ingestion.aai_traffic import ingest
from services.warehouse.loader import WarehouseLoader, batched_upsert, get_engine
from services.warehouse.schema import OperatingMetricRow

log = get_logger(__name__)


def load_traffic(limit: int | None = None) -> dict:
    report = ingest(limit=limit)
    rows = report.pop("_rows", [])
    if not rows:
        report["rows_written"] = 0
        return report

    loader = WarehouseLoader()
    staged: list[dict] = []

    with Session(get_engine()) as s:
        run_id = s.execute(text("""
            INSERT INTO ingest_run (started_at, status, facts_loaded)
            VALUES (now(), 'RUNNING', 0) RETURNING ingest_run_id
        """)).scalar_one()
        s.commit()

        doc_ids: dict[str, int] = {}
        for r in rows:
            if r.doc_key not in doc_ids:
                doc_ids[r.doc_key] = loader._upsert_document(
                    s, r.doc_key,
                    {
                        "publisher": "AAI",
                        "title": f"AAI traffic {r.doc_key}",
                        "source_url": r.source_url,
                        "media_type": "application/pdf",
                        "sha256": hashlib.sha256(r.source_url.encode()).hexdigest(),
                    },
                    run_id,
                )
            # An airport without a resolved IATA code has no stable key to
            # join on, so it is skipped rather than keyed by a display name
            # that changes between releases.
            if not r.airport_iata:
                continue
            staged.append({
                "grain": "AIRPORT",
                "entity_key": r.airport_iata,
                "period_id": loader._upsert_period(s, r.period),
                "direction": r.direction,
                "metric": r.metric,
                "value": round(r.value, 4),
                "unit": r.unit,
                "source_document_id": doc_ids[r.doc_key],
            })
        s.commit()

        written = batched_upsert(
            s, OperatingMetricRow, "operating_metric_natural_key",
            ["value", "unit", "source_document_id"],
            staged,
            lambda r: (r["grain"], r["entity_key"], r["period_id"],
                       r["direction"], r["metric"]),
        )
        s.execute(text("UPDATE ingest_run SET status='COMPLETE', finished_at=now(), "
                       "facts_loaded=:n WHERE ingest_run_id=:i"),
                  {"i": run_id, "n": written})
        s.commit()

    report["rows_written"] = written
    report["documents_registered"] = len(doc_ids)
    log.info(f"AAI traffic: {written} metric row(s) from {len(doc_ids)} document(s)")
    return report
