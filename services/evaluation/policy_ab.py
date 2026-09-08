"""Does a model actually choose better tools than the rules?

The agent layer was built with two interchangeable policies and the claim
that either could drive it. That claim was never measured, and for the
whole life of the project it was also false in a way nobody could see: the
model endpoint was unreachable, every decision fell through to the
heuristic, and the trace still recorded the run as model-driven.

This harness answers the question the architecture implies. Same goal, same
tools, same documents, one variable. It reports step count, success rate,
wall time and token spend per policy, and it writes both sides into
`agent_run` under their own trace ids so the console can show them
side by side.

    python -m services.evaluation.policy_ab --limit 8
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.agents.base import Agent
from services.agents.extraction_agent import ExtractionAgent
from services.agents.policy import BedrockPolicy, HeuristicPolicy
from services.common.bedrock import get_client
from services.common.config import SETTINGS
from services.common.logging import get_logger, redact
from services.common.models import DocStatus, Publisher, SourceDocument
from services.warehouse.loader import get_engine
from services.warehouse.traces import new_trace_id, persist_runs

log = get_logger(__name__)


@dataclass
class ArmResult:
    policy: str
    runs: list = field(default_factory=list)
    wall_ms: int = 0
    model_calls: int = 0
    fallbacks: int = 0
    tokens: dict = field(default_factory=dict)

    def summary(self) -> dict:
        steps = [r.steps for r in self.runs]
        ok = [r for r in self.runs if r.succeeded]
        return {
            "policy": self.policy,
            "documents": len(self.runs),
            "succeeded": len(ok),
            "success_rate": round(len(ok) / len(self.runs), 3) if self.runs else 0.0,
            "total_steps": sum(steps),
            "mean_steps": round(statistics.mean(steps), 2) if steps else 0.0,
            "median_steps": statistics.median(steps) if steps else 0,
            "wall_ms": self.wall_ms,
            "ms_per_document": round(self.wall_ms / len(self.runs)) if self.runs else 0,
            "model_calls": self.model_calls,
            "fallbacks": self.fallbacks,
            "tokens": self.tokens,
        }


def _documents(limit: int) -> list[SourceDocument]:
    """Documents already on disk, so the comparison measures the policy.

    Re-downloading would put network variance on the critical path and
    make two arms differ for reasons that have nothing to do with which
    policy chose the tools.
    """
    with Session(get_engine()) as s:
        rows = s.execute(text("""
            SELECT source_url, publisher, media_type, raw_path, sha256, byte_size
            FROM source_document
            WHERE raw_path IS NOT NULL AND media_type LIKE '%pdf%'
            ORDER BY source_document_id DESC LIMIT :k
        """), {"k": limit}).mappings().all()

    return [
        SourceDocument(
            publisher=Publisher(r["publisher"]),
            source_url=r["source_url"],
            sha256=r["sha256"],
            media_type=r["media_type"],
            byte_size=r["byte_size"],
            raw_path=r["raw_path"],
            status=DocStatus.DISCOVERED,
        )
        for r in rows
    ]


def _run_arm(policy_name: str, docs: list[SourceDocument]) -> ArmResult:
    arm = ArmResult(policy=policy_name)
    client = get_client()
    before = client.usage.to_dict()
    t0 = time.perf_counter()

    for doc in docs:
        agent = ExtractionAgent()
        # Swap the policy on an otherwise identical agent. Constructing a
        # different agent per arm would vary the tools as well as the
        # decision function and measure nothing in particular.
        heuristic = HeuristicPolicy(agent._plan)
        if policy_name == "heuristic":
            agent.policy = heuristic
        else:
            agent.policy = BedrockPolicy(fallback=heuristic)

        run = agent.run(f"extract cargo facts from {redact(doc.source_url)}", document=doc)
        arm.runs.append(run)
        if isinstance(agent.policy, BedrockPolicy):
            arm.model_calls += agent.policy.model_count
            arm.fallbacks += agent.policy.fallback_count

    arm.wall_ms = int((time.perf_counter() - t0) * 1000)
    after = client.usage.to_dict()
    arm.tokens = {k: after[k] - before.get(k, 0) for k in after}
    return arm


def compare(limit: int = 8, persist: bool = True) -> dict:
    docs = _documents(limit)
    if not docs:
        return {"error": "no archived documents to replay"}

    log.info(f"policy A/B over {len(docs)} document(s)")
    arms = {name: _run_arm(name, docs) for name in ("heuristic", "llm")}

    if persist:
        with Session(get_engine()) as s:
            for name, arm in arms.items():
                persist_runs(s, arm.runs, f"ab-{name}-{new_trace_id()[:8]}")

    h, m = arms["heuristic"].summary(), arms["llm"].summary()
    verdict = _verdict(h, m)
    return {
        "documents": len(docs),
        "model_configured": SETTINGS.bedrock_model_id,
        "model_reachable": get_client().available,
        "arms": [h, m],
        "verdict": verdict,
    }


def _verdict(h: dict, m: dict) -> str:
    """State what the numbers support, and nothing beyond it."""
    if m["model_calls"] == 0:
        return (
            "Not a comparison. No model call succeeded, so both arms were "
            "decided by the same deterministic policy and the difference "
            "between them is measurement noise. Configure credentials and "
            "re-run before reading anything into these figures."
        )
    parts = []
    if m["success_rate"] > h["success_rate"]:
        parts.append(f"model extracted more documents ({m['success_rate']:.0%} vs {h['success_rate']:.0%})")
    elif m["success_rate"] < h["success_rate"]:
        parts.append(f"model extracted fewer documents ({m['success_rate']:.0%} vs {h['success_rate']:.0%})")
    else:
        parts.append(f"identical success rate ({h['success_rate']:.0%})")
    parts.append(
        f"{m['mean_steps']:.1f} vs {h['mean_steps']:.1f} mean steps, "
        f"{m['ms_per_document']} vs {h['ms_per_document']} ms per document"
    )
    if m["fallbacks"]:
        parts.append(f"{m['fallbacks']} decision(s) still fell back to the rules")
    return "; ".join(parts) + "."


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare agent policies")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()
    print(json.dumps(compare(limit=args.limit, persist=not args.no_persist), indent=2))


if __name__ == "__main__":
    main()
