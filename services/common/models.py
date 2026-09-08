"""Domain models shared across ingestion and the agent pipeline.

Every model here exists to serve one invariant: a stored number must be
traceable to the document it came from and to the decisions that produced
it. That is why `SourceDocument` carries a checksum, `ExtractionResult`
carries a confidence and a parser name, and `CargoFact` carries both.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from services.common.logging import redact


def utcnow() -> datetime:
    return datetime.now(UTC)


class Publisher(str, Enum):
    AAI = "AAI"
    DGCA = "DGCA"
    DATA_GOV_IN = "DATA_GOV_IN"
    EUROSTAT = "EUROSTAT"
    WORLD_BANK = "WORLD_BANK"
    OPENFLIGHTS = "OPENFLIGHTS"
    OPERATOR = "OPERATOR"


class Direction(str, Enum):
    INTERNATIONAL = "INTERNATIONAL"
    DOMESTIC = "DOMESTIC"
    TOTAL = "TOTAL"


class DocStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    FETCHED = "FETCHED"
    PARSED = "PARSED"
    QUARANTINED = "QUARANTINED"
    FAILED = "FAILED"


@dataclass
class SourceDocument:
    """One artefact retrieved from a publisher, with its provenance."""

    publisher: Publisher
    source_url: str
    # Populated once fetched.
    sha256: str | None = None
    media_type: str | None = None
    byte_size: int | None = None
    raw_path: str | None = None
    published_on: str | None = None
    status: DocStatus = DocStatus.DISCOVERED
    # Free-form hints the discovery agent attaches (period, annex number...).
    hints: dict[str, Any] = field(default_factory=dict)
    discovered_at: datetime = field(default_factory=utcnow)
    note: str | None = None

    @property
    def doc_id(self) -> str:
        """Stable identity. Content hash once known, else URL hash."""
        basis = self.sha256 or hashlib.sha256(self.source_url.encode()).hexdigest()
        return basis[:16]

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the provenance ledger.

        The URL is redacted because credentials travel in query strings and
        the ledger is a durable, shareable artefact.
        """
        d = asdict(self)
        d["source_url"] = redact(self.source_url)
        d["publisher"] = self.publisher.value
        d["status"] = self.status.value
        d["discovered_at"] = self.discovered_at.isoformat()
        d["doc_id"] = self.doc_id
        return d


class Grain(str, Enum):
    """What a fact is measured *per*.

    The sources do not agree on this, and pretending they do would be a
    mistake. AAI publishes tonnage per airport per month; the Open
    Government Data platform publishes cargo per airline per year and
    carries no airport column at all. Both are real cargo facts, so the
    grain is recorded rather than one of them being forced into the
    other's shape or discarded.
    """

    AIRPORT = "AIRPORT"
    AIRLINE = "AIRLINE"


@dataclass
class CargoFact:
    """A single reconciled cargo measurement.

    Tonnage is always kilograms. Sources publish kg, MT and unqualified
    "tonnes"; normalising at write time makes every downstream aggregation
    a plain SUM and removes an entire class of unit bug.
    """

    airport_name_raw: str
    period: str                      # ISO month, e.g. "2026-04"
    direction: Direction
    tonnage_kg: float
    source_document_id: str
    publisher: Publisher
    airport_iata: str | None = None
    airport_icao: str | None = None
    airport_name: str | None = None
    country: str | None = None
    prior_year_tonnage_kg: float | None = None
    reported_change_pct: float | None = None
    resolution_confidence: float = 0.0
    resolution_method: str | None = None
    grain: Grain = Grain.AIRPORT
    airline: str | None = None
    # True when the row is an industry total ("All Scheduled Indian
    # Airlines") rather than one carrier. Adding those to per-carrier rows
    # double-counts the whole market, so they are labelled, not dropped -
    # the totals are useful on their own.
    is_aggregate: bool = False
    # Names the series, so two different measurements of the same entity
    # in the same period do not collapse onto one another.
    measure: str = "freight"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction.value
        d["publisher"] = self.publisher.value
        d["grain"] = self.grain.value
        return d


@dataclass
class ExtractionResult:
    """What a parser produced, and how much it trusts itself.

    `confidence` is what the extraction agent reflects on. A parser that
    returns rows it is unsure about is more useful than one that throws,
    because the agent can then try a different parser and compare.
    """

    parser: str
    facts: list[CargoFact] = field(default_factory=list)
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    # Audit trail that is not a problem: the column mapping a parser chose,
    # for example. Kept separate so recording a decision does not lower the
    # confidence score the way a real warning should.
    notes: list[str] = field(default_factory=list)
    rows_seen: int = 0
    rows_kept: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.facts) and self.confidence > 0

    def summary(self) -> str:
        return (
            f"{self.parser}: {self.rows_kept}/{self.rows_seen} rows kept, "
            f"confidence={self.confidence:.2f}, warnings={len(self.warnings)}, "
            f"notes={len(self.notes)}"
        )


@dataclass
class ToolCall:
    """One action an agent took, recorded so the run can be replayed."""

    tool: str
    args: dict[str, Any]
    ok: bool
    observation: str
    elapsed_ms: int = 0
    # Why the policy chose this call. Previously computed and discarded,
    # which made a trace a log of *what* happened with no record of the
    # judgement behind it - the part that actually needs auditing.
    reasoning: str = ""
    # Which policy produced this decision. Recorded per call, not per run,
    # because an LLM policy that falls back mid-run would otherwise label
    # heuristic decisions as model decisions.
    policy: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["args"] = {k: redact(v) if isinstance(v, str) else v for k, v in self.args.items()}
        d["observation"] = redact(self.observation)
        d["reasoning"] = redact(self.reasoning)
        return d


@dataclass
class AgentRun:
    """The full decision trace of one agent invocation.

    This is the artefact that makes agent judgment auditable rather than
    trusted: every tool call, in order, with what came back.
    """

    agent: str
    goal: str
    policy: str
    calls: list[ToolCall] = field(default_factory=list)
    succeeded: bool = False
    result_summary: str = ""
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    def record(self, call: ToolCall) -> None:
        self.calls.append(call)

    def finish(self, succeeded: bool, summary: str) -> AgentRun:
        self.succeeded = succeeded
        self.result_summary = summary
        self.finished_at = utcnow()
        return self

    @property
    def steps(self) -> int:
        return len(self.calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "goal": redact(self.goal),
            "policy": self.policy,
            "succeeded": self.succeeded,
            "steps": self.steps,
            "result_summary": self.result_summary,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "calls": [c.to_dict() for c in self.calls],
        }
