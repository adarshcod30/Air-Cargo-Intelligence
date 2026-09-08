"""Parser registry. The extraction agent asks this what it may try."""

from __future__ import annotations

from services.ingestion.parsers.aai_freight import AAIFreightParser
from services.ingestion.parsers.base import Parser
from services.ingestion.parsers.datagovin import DataGovInParser
from services.ingestion.parsers.eurostat_freight import EurostatFreightParser


def all_parsers() -> list[Parser]:
    return [AAIFreightParser(), EurostatFreightParser(), DataGovInParser()]


def by_name(name: str) -> Parser | None:
    return next((p for p in all_parsers() if p.name == name), None)


def ranked_for(doc, payload: bytes) -> list[tuple[Parser, float]]:
    """Parsers ordered by self-reported suitability.

    The agent uses this as a starting order, not as an answer - a parser
    that claims 0.9 and then yields nothing gets abandoned for the next.
    """
    scored = [(p, p.can_handle(doc, payload)) for p in all_parsers()]
    return sorted([s for s in scored if s[1] > 0], key=lambda s: -s[1])
