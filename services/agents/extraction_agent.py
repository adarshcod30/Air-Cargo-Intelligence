"""Extraction agent: gets facts out of a document, or explains why it can't.

The reflection here is the point. A parser is not trusted because it ran
without raising - it is scored, and if the score is below the floor the
agent tries the next candidate and keeps the best result. When nothing
clears the floor, the document is quarantined with the evidence attached
rather than being dropped or, worse, half-ingested.

This is the loop that catches AAI serving an HTML error page as a .pdf:
`probe_document` sees text/html where the URL promised a PDF, and the
agent quarantines instead of feeding garbage downstream.
"""

from __future__ import annotations

from typing import Any

from services.agents.base import Agent, Decision
from services.agents.policy import default_policy
from services.common.config import SETTINGS
from services.common.logging import get_logger, redact
from services.common.models import (
    DocStatus,
    ExtractionResult,
    SourceDocument,
    ToolCall,
)
from services.ingestion.fetcher import fetch, sniff_media_type
from services.ingestion.parsers import registry as parser_registry
from services.ingestion.store import RawStore

log = get_logger(__name__)


class ExtractionAgent(Agent):
    name = "extraction"

    def __init__(self, store: RawStore | None = None) -> None:
        super().__init__(policy=default_policy(self._plan))
        self.store = store or RawStore()
        self._register_tools()

    # ----------------------------------------------------------- tools --

    def _register_tools(self) -> None:
        @self.tool("fetch_document", "Download the document and archive the raw bytes.")
        def fetch_document() -> str:
            doc: SourceDocument = self.context["document"]
            res = fetch(doc.source_url)
            if not res.ok:
                doc.status = DocStatus.FAILED
                raise RuntimeError(f"HTTP {res.status}")
            self.context["payload"] = res.payload
            self.context["fetch_warnings"] = res.warnings
            self.store.archive(doc, res.payload, res.media_type, res.sha256)
            return f"{res.size} bytes, {res.media_type}, {len(res.warnings)} warning(s)"

        @self.tool("probe_document", "Verify what the bytes actually are.")
        def probe_document() -> dict:
            doc: SourceDocument = self.context["document"]
            payload: bytes = self.context["payload"]
            actual = sniff_media_type(payload)
            expected_pdf = doc.source_url.lower().split("?")[0].endswith(".pdf")
            trap = expected_pdf and actual != "application/pdf"
            self.context["is_trap"] = trap
            return {
                "actual_type": actual,
                "url_promised_pdf": expected_pdf,
                "content_mismatch": trap,
            }

        @self.tool("rank_parsers", "Ask each parser how suited it is to this document.")
        def rank_parsers() -> list:
            doc, payload = self.context["document"], self.context["payload"]
            ranked = parser_registry.ranked_for(doc, payload)
            self.context["candidates"] = [p.name for p, _ in ranked]
            self.context["tried"] = self.context.get("tried", [])
            return [f"{p.name}:{s:.2f}" for p, s in ranked]

        @self.tool("run_parser", "Run one parser and score what it produced.",
                   parser="parser name")
        def run_parser(parser: str) -> dict:
            doc, payload = self.context["document"], self.context["payload"]
            impl = parser_registry.by_name(parser)
            if impl is None:
                raise ValueError(f"unknown parser {parser!r}")
            result: ExtractionResult = impl.parse(doc, payload)
            self.context.setdefault("tried", []).append(parser)
            self.context.setdefault("results", []).append(result)
            best = self.context.get("best")
            if best is None or result.confidence > best.confidence:
                self.context["best"] = result
            return {
                "parser": parser,
                "facts": len(result.facts),
                "confidence": result.confidence,
                "warnings": result.warnings[:3],
            }

        @self.tool("quarantine", "Park the document for review with a reason.",
                   reason="why it could not be extracted")
        def quarantine(reason: str) -> str:
            doc: SourceDocument = self.context["document"]
            doc.status = DocStatus.QUARANTINED
            doc.note = reason
            self.context["quarantined"] = True
            log.warning(f"quarantined {redact(doc.source_url)}: {reason}")
            return reason

        @self.tool("accept", "Accept the best extraction so far.")
        def accept() -> str:
            doc: SourceDocument = self.context["document"]
            best: ExtractionResult = self.context["best"]
            doc.status = DocStatus.PARSED
            self.context["accepted"] = True
            return f"accepted {best.parser} with {len(best.facts)} facts"

    # ---------------------------------------------------------- policy --

    def _plan(self, history: list[ToolCall], context: dict[str, Any]) -> Decision:
        done = [c.tool for c in history if c.ok]
        floor = SETTINGS.extraction_confidence_floor

        if context.get("accepted") or context.get("quarantined"):
            return Decision(None, {}, "terminal state reached")

        if "fetch_document" not in done:
            return Decision("fetch_document", {}, "need the bytes")

        if "probe_document" not in done:
            return Decision("probe_document", {}, "verify the bytes match the promise")

        # Reflection 1: the content-mismatch trap.
        if context.get("is_trap"):
            return Decision(
                "quarantine",
                {"reason": "URL promised a PDF but the bytes are not a PDF; "
                           "likely an error page served with HTTP 200"},
                "content mismatch detected",
            )

        if "rank_parsers" not in done:
            return Decision("rank_parsers", {}, "choose candidate parsers")

        best = context.get("best")
        if best is not None and best.confidence >= floor:
            return Decision("accept", {}, f"confidence {best.confidence:.2f} clears {floor}")

        # Reflection 2: try the next untried candidate.
        tried = set(context.get("tried", []))
        remaining = [p for p in context.get("candidates", []) if p not in tried]
        if remaining:
            why = ("first candidate" if not tried
                   else f"previous parser scored {best.confidence:.2f} < {floor}")
            return Decision("run_parser", {"parser": remaining[0]}, why)

        if best is not None and best.facts:
            return Decision(
                "quarantine",
                {"reason": f"best parser {best.parser} scored {best.confidence:.2f}, "
                           f"below floor {floor}"},
                "no parser cleared the floor",
            )
        return Decision("quarantine", {"reason": "no parser produced any facts"},
                        "extraction exhausted")

    # ------------------------------------------------------------------ #

    def is_goal_met(self, context: dict[str, Any]) -> bool:
        return bool(context.get("accepted"))

    def summarise(self, context: dict[str, Any]) -> str:
        if context.get("quarantined"):
            doc: SourceDocument = context["document"]
            return f"quarantined: {doc.note}"
        best = context.get("best")
        if best is None:
            return "no extraction"
        return f"{best.parser}: {len(best.facts)} facts at confidence {best.confidence:.2f}"
