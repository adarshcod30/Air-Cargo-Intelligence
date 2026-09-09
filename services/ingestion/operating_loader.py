"""Loading operating metrics into the warehouse.

Reads the archived payloads rather than the network: the documents are
already downloaded and already recorded in source_document, so this adds a
second reading of the same evidence rather than a second ingestion.

Provenance is carried through unchanged. Every metric row names the document
it came from, and the foreign key is NOT NULL for the same reason it is on
the facts - a measurement whose origin cannot be named should not be stored.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.ingestion.operating_metrics import extract_file
from services.warehouse.loader import WarehouseLoader, batched_upsert, get_engine
from services.warehouse.schema import OperatingMetricRow

log = get_logger(__name__)


def _resolve(raw_path: str) -> Path:
    p = Path(raw_path)
    return p if p.is_absolute() else Path(SETTINGS.raw_dir).parents[1] / p


def load_operating_metrics(rebuild: bool = False, limit: int | None = None) -> dict:
    engine = get_engine()
    loader = WarehouseLoader()
    seen_metrics: set[str] = set()
    staged: list[dict] = []
    files = 0

    with Session(engine) as s:
        if rebuild:
            s.execute(text("DELETE FROM fact_operating_metric"))
            s.commit()

        docs = s.execute(text("""
            SELECT source_document_id, doc_key, raw_path
            FROM source_document
            WHERE publisher = 'DATA_GOV_IN' AND raw_path IS NOT NULL
            ORDER BY source_document_id
        """)).mappings().all()
        if limit:
            docs = docs[:limit]

        for d in docs:
            path = _resolve(d["raw_path"])
            if not path.exists():
                continue
            metrics = extract_file(path, d["doc_key"])
            if not metrics:
                continue
            files += 1
            for m in metrics:
                pid = loader._upsert_period(s, m.period)
                staged.append({
                    "grain": m.grain, "entity_key": m.entity_key, "period_id": pid,
                    "direction": m.direction, "metric": m.metric,
                    "value": round(m.value, 4), "unit": m.unit,
                    "source_document_id": d["source_document_id"],
                })
                seen_metrics.add(m.metric)
        s.commit()

        written = batched_upsert(
            s, OperatingMetricRow, "operating_metric_natural_key",
            ["value", "unit", "source_document_id"],
            staged,
            lambda r: (r["grain"], r["entity_key"], r["period_id"],
                       r["direction"], r["metric"]),
        )
        s.commit()

    summary = {
        "documents_read": files,
        "rows_extracted": len(staged),
        "rows_written": written,
        "distinct_metrics": sorted(seen_metrics),
    }
    log.info(f"operating metrics: {summary['rows_written']} row(s) from {files} document(s)")
    return summary
