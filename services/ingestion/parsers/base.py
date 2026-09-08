"""Parser contract.

A parser reports how well it thinks it did instead of throwing. That is
what lets the extraction agent compare two parsers on the same document
and pick the better result, rather than accepting whichever one happened
not to raise.
"""

from __future__ import annotations

from typing import Protocol

from services.common.models import ExtractionResult, SourceDocument


class Parser(Protocol):
    name: str

    def can_handle(self, doc: SourceDocument, payload: bytes) -> float:
        """Confidence in [0,1] that this parser suits the document."""
        ...

    def parse(self, doc: SourceDocument, payload: bytes) -> ExtractionResult:
        """Extract facts. Should not raise for malformed input."""
        ...


def score_extraction(result: ExtractionResult) -> float:
    """Grade an extraction so the agent has something to reflect on.

    Deliberately blunt and deterministic. Yield dominates because the
    common real failure is a parser that "succeeds" while silently
    dropping most rows - a case that a pass/fail check misses entirely.
    """
    if not result.facts:
        return 0.0
    yield_ratio = result.rows_kept / result.rows_seen if result.rows_seen else 0.0
    penalty = min(0.3, 0.05 * len(result.warnings))
    # What counts as "resolved" depends on the grain. Requiring an airport
    # code scored the Eurostat parser at zero (it keys by ICAO) and then
    # scored a flawless airline-level extraction 0.55, below the floor,
    # because airline facts have no airport at all.
    from services.common.models import Grain

    def _resolved(f) -> bool:
        if f.grain is Grain.AIRLINE:
            return bool(f.airline)
        return bool(f.airport_iata or f.airport_icao)

    resolved = sum(1 for f in result.facts if _resolved(f))
    resolution = resolved / len(result.facts)
    return round(max(0.0, min(1.0, 0.6 * yield_ratio + 0.4 * resolution - penalty)), 3)
