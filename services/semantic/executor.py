"""Runs a compiled query and attaches the provenance behind its rows.

Citations are produced here rather than left to the caller, because a
number and the document that supports it should not be separable. Any
answer this layer returns can be traced to source without the caller
having to remember to ask.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.common.logging import get_logger
from services.semantic.compiler import CompiledQuery, QuerySpec, compile_query

log = get_logger(__name__)


@dataclass
class Citation:
    source_document_id: int
    publisher: str
    title: str | None
    source_url: str
    retrieved_at: str | None

    def to_dict(self) -> dict:
        return {
            "source_document_id": self.source_document_id,
            "publisher": self.publisher,
            "title": self.title,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
        }


@dataclass
class QueryResult:
    rows: list[dict[str, Any]]
    columns: list[str]
    citations: list[Citation] = field(default_factory=list)
    sql: str = ""
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "columns": self.columns,
            "citations": [c.to_dict() for c in self.citations],
            "explanation": self.explanation,
            "row_count": len(self.rows),
        }


_CITATION_SQL = text("""
    SELECT DISTINCT
        sd.source_document_id, sd.publisher, sd.title,
        sd.source_url, sd.retrieved_at
    FROM source_document sd
    WHERE sd.source_document_id IN (
        SELECT DISTINCT source_document_id FROM v_cargo_fact
        WHERE (CAST(:grain AS text) IS NULL OR grain = CAST(:grain AS text))
          AND (CAST(:period AS text) IS NULL OR period = CAST(:period AS text))
          AND (CAST(:direction AS text) IS NULL OR direction = CAST(:direction AS text))
        LIMIT 200
    )
    ORDER BY 1
    LIMIT :limit
""")


def run(session: Session, spec: QuerySpec, with_citations: bool = True) -> QueryResult:
    compiled: CompiledQuery = compile_query(spec)
    log.debug(f"semantic query: {compiled.sql}")

    rows = [
        {k: _jsonable(v) for k, v in r.items()}
        for r in session.execute(text(compiled.sql), compiled.params).mappings().all()
    ]

    citations: list[Citation] = []
    if with_citations and spec.source == "v_cargo_fact":
        cite_rows = session.execute(
            _CITATION_SQL,
            {
                "grain": spec.filters.get("grain"),
                "period": spec.filters.get("period"),
                "direction": spec.filters.get("direction"),
                "limit": 12,
            },
        ).mappings().all()
        citations = [
            Citation(
                source_document_id=r["source_document_id"],
                publisher=r["publisher"],
                title=r["title"],
                source_url=r["source_url"],
                retrieved_at=r["retrieved_at"].isoformat() if r["retrieved_at"] else None,
            )
            for r in cite_rows
        ]

    return QueryResult(
        rows=rows,
        columns=compiled.columns,
        citations=citations,
        sql=compiled.sql,
        explanation=compiled.explain(),
    )


def _jsonable(v: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v
