"""Policies: the part of an agent that decides what to do next.

Two implementations, one interface, one trace format. The heuristic policy
encodes the rules we already know; the model policy handles documents whose
shape we have not anticipated. Running the heuristic first and escalating
only on failure keeps model calls rare, which matters when a monthly crawl
touches hundreds of documents.

Every Decision carries the name of the policy that produced it. That field
exists because of a real defect: the model policy caught its own failures
and returned the heuristic's decision, but the run was still stamped
`policy: llm`, so 200 recorded runs claimed model judgement for decisions
made entirely by deterministic rules. A fallback must be visible in the
trace or the trace is not evidence.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from services.agents.base import Decision, Policy, Tool
from services.common.bedrock import BedrockUnavailable, get_client
from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.common.models import ToolCall

log = get_logger(__name__)

_SYSTEM = (
    "You are the control loop of a data-ingestion agent. Your only job is "
    "to choose the next tool to call, or to stop.\n\n"
    "You must never output a measurement, a number read from a document, or "
    "any analytical conclusion. Numbers come from parsers and from SQL, "
    "never from you. If you are tempted to state a figure, choose a tool "
    "that retrieves it instead."
)


class HeuristicPolicy:
    """A deterministic plan with reflection at the branch points.

    `plan` receives the history and context and returns the next Decision.
    This is not "just a script": the plan branches on observations, which
    is how the agent recovers from a parser that failed or a URL pattern
    that returned nothing.
    """

    def __init__(self, plan: Callable[[list[ToolCall], dict[str, Any]], Decision]) -> None:
        self._plan = plan
        self.name = "heuristic"

    def decide(
        self,
        goal: str,
        tools: dict[str, Tool],
        history: list[ToolCall],
        context: dict[str, Any],
    ) -> Decision:
        d = self._plan(history, context)
        d.policy = d.policy or self.name
        return d


class BedrockPolicy:
    """Asks a managed model to choose the next tool.

    Deliberately constrained: the model returns a tool name and arguments,
    both validated against the registered allowlist before anything runs.
    It cannot return a measurement, a SQL statement, or free-form code, so
    a bad completion costs a wasted step rather than a corrupted fact.
    """

    def __init__(self, fallback: Policy | None = None, model_id: str | None = None) -> None:
        self.name = "llm"
        self.fallback = fallback
        self.model_id = model_id or SETTINGS.bedrock_model_id
        self._client = get_client()
        self.fallback_count = 0
        self.model_count = 0

    @property
    def usage(self) -> dict[str, int]:
        return self._client.usage.to_dict()

    def _fall_back(
        self, why: str, goal: str, tools: dict[str, Tool],
        history: list[ToolCall], context: dict[str, Any],
    ) -> Decision:
        self.fallback_count += 1
        if self.fallback is None:
            return Decision(None, {}, f"no model and no fallback ({why})", policy="none")
        d = self.fallback.decide(goal, tools, history, context)
        # Relabelled so the console shows a heuristic decision as heuristic
        # even though the agent was configured to use a model.
        d.policy = f"heuristic(fallback:{why})"
        return d

    def decide(
        self,
        goal: str,
        tools: dict[str, Tool],
        history: list[ToolCall],
        context: dict[str, Any],
    ) -> Decision:
        if not self._client.available:
            return self._fall_back("no-credentials", goal, tools, history, context)

        catalogue = [
            {"name": t.name, "description": t.description, "params": t.params}
            for t in tools.values()
        ]
        transcript = [
            {"tool": c.tool, "args": c.args, "ok": c.ok, "observation": c.observation[:300]}
            for c in history[-8:]
        ]
        prompt = (
            f"GOAL: {goal}\n\n"
            f"TOOLS: {json.dumps(catalogue, indent=2)}\n\n"
            f"STEPS SO FAR: {json.dumps(transcript, indent=2, default=str)}\n\n"
            'Reply with JSON only: {"tool": "<name or null>", '
            '"args": {...}, "reasoning": "<one sentence>"}'
        )

        try:
            parsed = self._client.converse_json(
                prompt, system=_SYSTEM, max_tokens=512, temperature=0.0,
                model_id=self.model_id,
            )
        except BedrockUnavailable as exc:
            log.warning(f"model policy unavailable ({exc}); falling back")
            return self._fall_back("model-error", goal, tools, history, context)
        except Exception as exc:  # malformed JSON, unexpected shape
            log.warning(f"model policy returned unusable output ({type(exc).__name__})")
            return self._fall_back("bad-output", goal, tools, history, context)

        tool_name = parsed.get("tool")
        if tool_name is not None and tool_name not in tools:
            # Reject rather than trust. The loop turns this into a
            # corrective observation and the model gets another turn.
            log.warning(f"model proposed unknown tool {tool_name!r}; rejecting")

        self.model_count += 1
        return Decision(
            tool=tool_name,
            args=parsed.get("args") or {},
            reasoning=parsed.get("reasoning", ""),
            policy=f"llm:{self.model_id}",
        )


# Kept as an alias so existing imports and tests continue to resolve.
LLMPolicy = BedrockPolicy


def default_policy(plan: Callable[[list[ToolCall], dict[str, Any]], Decision]) -> Policy:
    """Model policy when one is genuinely reachable, heuristic otherwise.

    Escalation order matters for cost: a monthly crawl over hundreds of
    documents should not make hundreds of model calls to rediscover rules
    we already encoded.
    """
    heuristic = HeuristicPolicy(plan)
    if SETTINGS.llm_available:
        return BedrockPolicy(fallback=heuristic)
    return heuristic
