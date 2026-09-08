"""Request and response contracts.

Responses carry their citations and the plain-language explanation of
what was run, so a consumer can always show where a number came from
without making a second call for it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CitationOut(BaseModel):
    source_document_id: int
    publisher: str
    title: str | None = None
    source_url: str
    retrieved_at: str | None = None


class QueryResponse(BaseModel):
    rows: list[dict[str, Any]]
    columns: list[str]
    row_count: int
    explanation: str = Field(
        description="Plain-language account of the query that produced these rows."
    )
    citations: list[CitationOut] = []


class ChatRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class ChatResponse(BaseModel):
    answer: str
    intent: str
    understood_as: str = Field(
        description="How the question was interpreted, so a wrong reading is visible."
    )
    rows: list[dict[str, Any]]
    chart: dict[str, Any] | None = None
    citations: list[CitationOut] = []
    grounded: bool = Field(
        description="True when every figure in the answer came from a stored row."
    )
    passages: list[dict[str, Any]] = Field(
        default=[],
        description=(
            "Source paragraphs retrieved for the question. Context and "
            "provenance only - no figure in the answer comes from them."
        ),
    )
    retrieval: dict[str, Any] | None = Field(
        default=None, description="Which retriever and embedding model served the passages."
    )


class HealthResponse(BaseModel):
    status: str
    database: str
    facts: int
    airports: int
    airlines: int
    periods: int
