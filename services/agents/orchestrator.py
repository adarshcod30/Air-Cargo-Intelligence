"""Pipeline: discovery -> extraction -> reconciliation, with full traces.

Every agent run is written to `data/processed/agent_runs.jsonl`, so a
month's ingest can be audited step by step long after it finished. That
trace is what makes agent judgement reviewable instead of trusted.

Usage:
    python -m services.agents.orchestrator --source aai_freight --limit 3
    python -m services.agents.orchestrator --all --limit 2
    python -m services.agents.orchestrator --seed
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from services.agents.discovery_agent import DiscoveryAgent
from services.agents.extraction_agent import ExtractionAgent
from services.agents.reconciliation_agent import ReconciliationAgent
from services.common.config import SETTINGS
from services.common.logging import get_logger, redact
from services.common.models import AgentRun, CargoFact, SourceDocument
from services.ingestion import registry as source_registry
from services.ingestion.seed import build_airport_crosswalk
from services.ingestion.store import RawStore
from services.warehouse.traces import new_trace_id

log = get_logger(__name__)


@dataclass
class PipelineReport:
    documents_discovered: int = 0
    documents_extracted: int = 0
    documents_quarantined: int = 0
    facts_extracted: int = 0
    facts_reconciled: int = 0
    facts_quarantined: int = 0
    review_queue: list[str] = field(default_factory=list)
    collisions: dict = field(default_factory=dict)
    runs: list[AgentRun] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "documents_discovered": self.documents_discovered,
            "documents_extracted": self.documents_extracted,
            "documents_quarantined": self.documents_quarantined,
            "facts_extracted": self.facts_extracted,
            "facts_reconciled": self.facts_reconciled,
            "facts_quarantined": self.facts_quarantined,
            "review_queue": self.review_queue[:50],
            "collisions": dict(list(self.collisions.items())[:40]),
            "collision_count": len(self.collisions),
            "agent_runs": [r.to_dict() for r in self.runs],
        }


class Pipeline:
    def __init__(self) -> None:
        self.store = RawStore()
        self.report = PipelineReport()
        # One id shared by every agent in this invocation, so the console
        # can group a pipeline run rather than showing a flat list of
        # unrelated agents.
        self.trace_id = new_trace_id()

    # ------------------------------------------------------------------ #

    def run_source(self, key: str, limit: int | None = None) -> list[CargoFact]:
        source = source_registry.get(key)
        if source is None:
            raise ValueError(f"unknown source {key!r}")
        log.info(f"=== source: {source.key} ({source.status.value}) ===")

        docs = self._discover(source)
        if limit:
            docs = docs[:limit]
        return self._extract_all(docs)

    def _discover(self, source) -> list[SourceDocument]:
        if source.key == "data_gov_in_cargo":
            return self._discover_ogd(source)
        if source.api_template and source.api_params:
            return self._discover_api(source)
        if not source.index_url:
            log.info(f"{source.key}: no index URL and no API template; skipping")
            return []
        agent = DiscoveryAgent(source)
        run = agent.run(f"find cargo documents published by {source.publisher.value}")
        self.report.runs.append(run)
        docs: list[SourceDocument] = agent.context.get("documents", []) or []
        self.report.documents_discovered += len(docs)
        log.info(f"discovery: {run.result_summary}")
        return docs

    def _discover_ogd(self, source) -> list[SourceDocument]:
        """Discover air-cargo datasets from the OGD catalogue.

        The platform has no server-side sector filter on its list endpoint,
        so the index is paged once, cached, and filtered locally. Each
        surviving resource keeps the terms that matched, so the decision to
        include a dataset can be reviewed rather than taken on trust.
        """
        from services.ingestion.datagovin_catalog import MissingApiKey, OgdCatalogue

        try:
            catalogue = OgdCatalogue()
            candidates = catalogue.air_cargo_resources()
        except MissingApiKey as exc:
            log.warning(f"{source.key}: {exc}")
            return []
        except Exception as exc:
            log.error(f"{source.key}: catalogue unavailable: {exc}")
            return []

        docs = [
            SourceDocument(
                publisher=source.publisher,
                source_url=res.api_url(catalogue.api_key or ""),
                hints={
                    "source_key": source.key,
                    "resource_id": res.resource_id,
                    "title": res.title,
                    "matched_terms": evidence,
                },
            )
            for res, evidence in candidates
        ]
        self.report.documents_discovered += len(docs)
        log.info(f"{source.key}: {len(docs)} air-cargo dataset(s) discovered")
        return docs

    def _discover_api(self, source) -> list[SourceDocument]:
        """Enumerate documents for a parameterised API.

        There is nothing to crawl, so discovery is a product over the
        registered parameters. Eurostat rejects unfiltered queries with
        HTTP 413, which is why the airport list is explicit rather than
        being discovered.
        """
        import itertools

        keys = list(source.api_params)
        docs: list[SourceDocument] = []
        for combo in itertools.product(*(source.api_params[k] for k in keys)):
            params = dict(zip(keys, combo, strict=True))
            docs.append(
                SourceDocument(
                    publisher=source.publisher,
                    source_url=source.api_template.format(**params),
                    hints={"source_key": source.key, **params},
                )
            )
        self.report.documents_discovered += len(docs)
        log.info(f"{source.key}: {len(docs)} API document(s) enumerated")
        return docs

    def _extract_all(self, docs: list[SourceDocument]) -> list[CargoFact]:
        """Extract every document, but stop early if the source gives up.

        A circuit breaker matters here because these are public endpoints
        with real limits. Grinding through 146 documents that are all
        being refused wastes time and is rude to the publisher, so a run
        of consecutive rate limits aborts the source with a clear message
        rather than completing as a long list of failures.
        """
        facts: list[CargoFact] = []
        consecutive_rate_limits = 0
        for i, doc in enumerate(docs, start=1):
            if consecutive_rate_limits >= 3:
                remaining = len(docs) - i + 1
                log.error(
                    f"circuit breaker: {consecutive_rate_limits} consecutive rate "
                    f"limits, abandoning {remaining} remaining document(s). "
                    f"Wait for the limit to reset and re-run."
                )
                self.report.documents_quarantined += remaining
                break
            label = doc.hints.get("title") or redact(doc.source_url).rsplit("/", 1)[-1]
            log.info(f"--- document {i}/{len(docs)}: {label[:80]}")
            agent = ExtractionAgent(store=self.store)
            run = agent.run(f"extract cargo facts from {redact(doc.source_url)}", document=doc)
            self.report.runs.append(run)
            self.store.record(doc)

            if agent.context.get("rate_limited"):
                consecutive_rate_limits += 1
            else:
                consecutive_rate_limits = 0

            if run.succeeded and agent.context.get("best"):
                best = agent.context["best"]
                facts.extend(best.facts)
                self.report.documents_extracted += 1
                self.report.facts_extracted += len(best.facts)
            else:
                self.report.documents_quarantined += 1
        return facts

    # ------------------------------------------------------------------ #

    def reconcile(self, facts: list[CargoFact]) -> list[CargoFact]:
        if not facts:
            return []
        agent = ReconciliationAgent()
        run = agent.run("canonicalise airports, units and periods", facts=facts)
        self.report.runs.append(run)
        accepted = agent.context.get("accepted", [])
        self.report.facts_reconciled = len(accepted)
        self.report.facts_quarantined = len(agent.context.get("quarantined", []))
        self.report.review_queue = agent.context.get("review_queue", [])
        # Collisions are the main reason a fact gets quarantined, so the
        # detail belongs in the report rather than being re-derived by
        # hand every time the number looks wrong.
        self.report.collisions = agent.context.get("collisions", {})
        log.info(f"reconciliation: {run.result_summary}")
        return accepted

    def persist(self, facts: list[CargoFact]) -> Path:
        out_dir = Path(SETTINGS.processed_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        facts_path = out_dir / "cargo_facts.jsonl"
        with facts_path.open("w", encoding="utf-8") as fh:
            for f in facts:
                fh.write(json.dumps(f.to_dict(), default=str) + "\n")

        runs_path = out_dir / "agent_runs.jsonl"
        with runs_path.open("w", encoding="utf-8") as fh:
            for r in self.report.runs:
                fh.write(json.dumps(r.to_dict(), default=str) + "\n")

        (out_dir / "pipeline_report.json").write_text(
            json.dumps(self.report.to_dict(), indent=2, default=str), encoding="utf-8"
        )
        self._persist_traces()
        log.info(f"wrote {len(facts)} facts -> {facts_path}")
        return facts_path

    def _persist_traces(self) -> None:
        """Mirror the run traces into the warehouse.

        The JSONL stays: it is the record of last resort when the database
        is unreachable, and it is what the backfill reads. But a trace only
        becomes usable - queryable, joinable, servable to the console - once
        it is a row, so the database write is the one that matters and a
        failure here is logged rather than allowed to lose an ingest.
        """
        if not self.report.runs:
            return
        try:
            from sqlalchemy.orm import Session

            from services.warehouse.loader import get_engine
            from services.warehouse.traces import persist_runs

            with Session(get_engine()) as session:
                persist_runs(session, self.report.runs, self.trace_id)
        except Exception as exc:
            log.warning(f"could not persist traces to warehouse: {type(exc).__name__}: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Air Cargo Intelligence ingestion pipeline")
    ap.add_argument("--source", help="registry key, e.g. aai_freight")
    ap.add_argument("--all", action="store_true", help="run every ACTIVE source")
    ap.add_argument("--limit", type=int, default=None, help="max documents per source")
    ap.add_argument("--seed", action="store_true", help="(re)build the airport crosswalk")
    args = ap.parse_args()

    if args.seed:
        build_airport_crosswalk()
        if not (args.source or args.all):
            return

    pipeline = Pipeline()
    facts: list[CargoFact] = []

    keys = (
        [s.key for s in source_registry.active() if s.key != "openflights_airports"]
        if args.all
        else [args.source] if args.source
        else []
    )
    if not keys:
        ap.error("choose --source <key> or --all")

    for key in keys:
        try:
            facts.extend(pipeline.run_source(key, limit=args.limit))
        except Exception as exc:
            log.error(f"source {key} failed: {type(exc).__name__}: {exc}")

    reconciled = pipeline.reconcile(facts)
    pipeline.persist(reconciled)

    r = pipeline.report
    print("\n" + "=" * 62)
    print("INGESTION SUMMARY")
    print("=" * 62)
    print(f"  documents discovered   {r.documents_discovered}")
    print(f"  documents extracted    {r.documents_extracted}")
    print(f"  documents quarantined  {r.documents_quarantined}")
    print(f"  facts extracted        {r.facts_extracted}")
    print(f"  facts reconciled       {r.facts_reconciled}")
    print(f"  facts quarantined      {r.facts_quarantined}")
    print(f"  agent runs traced      {len(r.runs)}")
    if r.review_queue:
        print(f"  needs manual mapping   {len(r.review_queue)}: {r.review_queue[:6]}")
    print("=" * 62)


if __name__ == "__main__":
    main()
