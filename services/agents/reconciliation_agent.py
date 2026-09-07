"""Reconciliation agent: canonicalise facts, quarantine what it cannot.

The decision this agent exists to make is *when to refuse*. Fuzzy-matching
an unknown airport name to the nearest string is how a dataset quietly
attributes Kolkata's tonnage to Kozhikode. So resolution below the
confidence floor produces a quarantined row and a review queue entry, not
a guess - and the review queue is an output, not an error.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from services.agents.base import Agent, Decision
from services.agents.policy import default_policy
from services.common.logging import get_logger
from services.common.models import CargoFact, ToolCall
from services.ingestion.normalise import AirportResolver

log = get_logger(__name__)

RESOLUTION_FLOOR = 0.86


class ReconciliationAgent(Agent):
    name = "reconciliation"

    def __init__(self, resolver: AirportResolver | None = None) -> None:
        super().__init__(policy=default_policy(self._plan))
        self.resolver = resolver or AirportResolver()
        self._register_tools()

    def _register_tools(self) -> None:
        @self.tool("load_crosswalk", "Load the airport reference table.")
        def load_crosswalk() -> int:
            return self.resolver.load()

        @self.tool("resolve_airports", "Resolve every fact's airport to a code.")
        def resolve_airports() -> dict:
            facts: list[CargoFact] = self.context["facts"]
            methods: Counter = Counter()
            for fact in facts:
                if fact.airport_iata:
                    methods[fact.resolution_method or "pre-resolved"] += 1
                    continue
                # A source that already supplies an authoritative code needs
                # no name matching. Eurostat keys airports by ICAO, so its
                # rows arrive resolved; matching 'AMSTERDAM/SCHIPHOL airport'
                # as free text would only add a chance to get it wrong.
                if fact.airport_icao:
                    enriched = self.resolver.resolve_icao(fact.airport_icao)
                    if enriched:
                        fact.airport_iata = enriched.get("iata") or None
                        fact.airport_name = enriched.get("airport_name") or fact.airport_name
                        fact.country = enriched.get("country") or fact.country
                    fact.resolution_confidence = 1.0
                    fact.resolution_method = "icao"
                    methods["icao"] += 1
                    continue
                rec, conf, method = self.resolver.resolve(
                    fact.airport_name_raw, country_hint=fact.country
                )
                methods[method.split(":")[0]] += 1
                if rec and conf >= RESOLUTION_FLOOR:
                    fact.airport_iata = rec.get("iata") or None
                    fact.airport_icao = rec.get("icao") or None
                    fact.airport_name = rec.get("airport_name") or fact.airport_name
                fact.resolution_confidence = conf
                fact.resolution_method = method
            self.context["resolution_methods"] = dict(methods)
            return dict(methods)

        @self.tool(
            "detect_collisions",
            "Find distinct source names that resolved to the same airport code.",
        )
        def detect_collisions() -> dict:
            """Catch two different airports being merged into one code.

            AAI lists 'BENGALURU (BIAL)' and 'BENGALURU (HAL)' as separate
            rows: different airports, same city. Stripping the bracket to
            match the city maps both onto BLR, which double-counts the
            city and attributes a general aviation field's tonnage to the
            main cargo hub.

            Curated aliases fix the four cases we know about; this catches
            the ones we do not. Comparison is scoped to a single period and
            direction, so a source renaming a row between months (bare
            'GOA' becoming 'GOA (DABOLIM)') is correctly not a collision.
            """
            facts: list[CargoFact] = self.context["facts"]
            groups: dict[tuple, set[str]] = {}
            for f in facts:
                if not f.airport_iata:
                    continue
                groups.setdefault(
                    (f.airport_iata, f.period, f.direction), set()
                ).add(f.airport_name_raw)

            # Two different names on one code (BENGALURU BIAL vs HAL).
            collided = {k: v for k, v in groups.items() if len(v) > 1}

            # And the other shape of the same bug: one name emitted twice
            # for the same key. The Eurostat parser once accepted three
            # freight measures at once, so Frankfurt appeared repeatedly
            # per year with near-identical tonnage. A name-based check
            # cannot see that, so count occurrences too.
            seen: dict[tuple, int] = {}
            for f in facts:
                if not f.airport_iata:
                    continue
                k = (f.airport_iata, f.period, f.direction)
                seen[k] = seen.get(k, 0) + 1
            for k, n in seen.items():
                if n > 1 and k not in collided:
                    collided[k] = {f"duplicated x{n}"}
            for f in facts:
                key = (f.airport_iata, f.period, f.direction)
                if key in collided:
                    # Refuse rather than pick one arbitrarily.
                    f.resolution_confidence = 0.0
                    f.resolution_method = (
                        f"collision:{sorted(collided[key])}"
                    )
            self.context["collisions"] = {
                f"{k[0]}|{k[1]}|{k[2].value if hasattr(k[2], 'value') else k[2]}":
                sorted(v) for k, v in collided.items()
            }
            if collided:
                log.warning(f"{len(collided)} airport-code collision(s) detected")
            return {"collisions": len(collided)}

        @self.tool("partition", "Split facts into accepted and quarantined.")
        def partition() -> dict:
            facts: list[CargoFact] = self.context["facts"]
            accepted = [f for f in facts if f.resolution_confidence >= RESOLUTION_FLOOR]
            quarantined = [f for f in facts if f.resolution_confidence < RESOLUTION_FLOOR]
            self.context["accepted"] = accepted
            self.context["quarantined"] = quarantined
            return {"accepted": len(accepted), "quarantined": len(quarantined)}

        @self.tool("build_review_queue", "List unresolved names for a human to map.")
        def build_review_queue() -> int:
            names = sorted({f.airport_name_raw for f in self.context.get("quarantined", [])})
            self.context["review_queue"] = names
            if names:
                log.warning(f"{len(names)} name(s) need manual mapping: {names[:8]}")
            return len(names)

    def _plan(self, history: list[ToolCall], context: dict[str, Any]) -> Decision:
        done = [c.tool for c in history if c.ok]
        if "load_crosswalk" not in done:
            return Decision("load_crosswalk", {}, "reference table required")
        if "resolve_airports" not in done:
            return Decision("resolve_airports", {}, "canonicalise identities")
        if "detect_collisions" not in done:
            return Decision("detect_collisions", {}, "check for merged airports")
        if "partition" not in done:
            return Decision("partition", {}, "separate trustworthy rows")
        if "build_review_queue" not in done:
            return Decision("build_review_queue", {}, "surface what needs a human")
        return Decision(None, {}, "reconciliation complete")

    def is_goal_met(self, context: dict[str, Any]) -> bool:
        return "review_queue" in context

    def summarise(self, context: dict[str, Any]) -> str:
        acc = len(context.get("accepted", []))
        qua = len(context.get("quarantined", []))
        total = acc + qua
        pct = (acc / total * 100) if total else 0.0
        return f"{acc}/{total} facts reconciled ({pct:.1f}%), {qua} quarantined"
