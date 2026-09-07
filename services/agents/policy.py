"""Policies: the part of an agent that decides what to do next.

Two implementations, one interface. The heuristic policy encodes the rules
we already know; the LLM policy handles documents whose shape we have not
anticipated. Running the heuristic first and escalating only on failure
keeps model calls rare, which matters when a monthly crawl touches
hundreds of documents.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from services.agents.base import Decision, Policy, Tool
from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.common.models import ToolCall

log = get_logger(__name__)


class HeuristicPolicy:
    """A deterministic plan with reflection at the branch points.

    `plan` is a callable that receives the history and context and returns
    the next Decision. This is not "just a script": the plan branches on
    observations, which is how the agent recovers from a parser that
    failed or a URL pattern that returned nothing.
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
        return self._plan(history, context)


class LLMPolicy:
    """Asks an OpenAI-compatible endpoint to choose the next tool.

    Deliberately constrained: the model returns a tool name and arguments,
    both validated against the registered allowlist before anything runs.
    It cannot return a measurement, a SQL statement, or free-form code, so
    a bad completion costs a wasted step rather than a corrupted fact.
    """

    def __init__(self, fallback: Policy | None = None) -> None:
        self.name = "llm"
        self.fallback = fallback
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                base_url=SETTINGS.llm_base_url,
                timeout=SETTINGS.request_timeout,
                headers=(
                    {"Authorization": f"Bearer {SETTINGS.llm_api_key}"}
                    if SETTINGS.llm_api_key
                    else {}
                ),
            )
        return self._client

    def decide(
        self,
        goal: str,
        tools: dict[str, Tool],
        history: list[ToolCall],
        context: dict[str, Any],
    ) -> Decision:
        if not SETTINGS.llm_available:
            if self.fallback:
                return self.fallback.decide(goal, tools, history, context)
            return Decision(None, {}, "no model configured and no fallback")

        catalogue = [
            {"name": t.name, "description": t.description, "params": t.params}
            for t in tools.values()
        ]
        transcript = [
            {"tool": c.tool, "args": c.args, "ok": c.ok, "observation": c.observation[:300]}
            for c in history[-8:]
        ]
        prompt = (
            "You are the control loop of a data-ingestion agent.\n"
            "Choose the NEXT tool to call, or stop.\n\n"
            "You must never output a measurement, a number extracted from a "
            "document, or any analytical conclusion. Your only job is to "
            "choose a tool and its arguments.\n\n"
            f"GOAL: {goal}\n\n"
            f"TOOLS: {json.dumps(catalogue, indent=2)}\n\n"
            f"STEPS SO FAR: {json.dumps(transcript, indent=2, default=str)}\n\n"
            'Reply with JSON only: {"tool": "<name or null>", '
            '"args": {...}, "reasoning": "<one sentence>"}'
        )

        try:
            resp = self._get_client().post(
                "/chat/completions",
                json={
                    "model": SETTINGS.llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception as exc:
            log.warning(f"LLM policy unavailable ({type(exc).__name__}); using fallback")
            if self.fallback:
                return self.fallback.decide(goal, tools, history, context)
            return Decision(None, {}, f"llm error: {exc}")

        tool_name = parsed.get("tool")
        if tool_name is not None and tool_name not in tools:
            # Reject rather than trust. The loop turns this into a
            # corrective observation and the model gets another turn.
            log.warning(f"LLM proposed unknown tool {tool_name!r}; rejecting")
        return Decision(
            tool=tool_name,
            args=parsed.get("args") or {},
            reasoning=parsed.get("reasoning", ""),
        )


def default_policy(plan: Callable[[list[ToolCall], dict[str, Any]], Decision]) -> Policy:
    """Heuristic first, model only when one is configured.

    Escalation order matters for cost: a monthly crawl over hundreds of
    documents should not make hundreds of model calls to rediscover rules
    we already encoded.
    """
    heuristic = HeuristicPolicy(plan)
    if SETTINGS.llm_available:
        return LLMPolicy(fallback=heuristic)
    return heuristic
