"""The agent loop: perceive, decide, act, observe, reflect.

What makes the agents here genuinely agentic rather than a pipeline of
functions is that none of them is told *which* steps to run. Each is given
a goal, a set of tools, and a budget, and decides the next action from
what it has observed so far - including deciding to give up and quarantine.

The decision function is a `Policy`, and it is pluggable on purpose:

  HeuristicPolicy  deterministic rules. No model needed, so the pipeline
                   runs in CI and on a laptop with no API key.
  LLMPolicy        asks a language model to choose the next tool.

Both emit the identical `AgentRun` trace, so a run is comparable and
replayable whichever policy produced it.

The grounding invariant is enforced structurally: a policy may only choose
a tool name and arguments from the registered allowlist. It never returns
a measurement. Numbers come from parsers; agents decide *how to get them*.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.common.models import AgentRun, ToolCall

log = get_logger(__name__)


@dataclass
class Tool:
    """One capability an agent may invoke."""

    name: str
    description: str
    fn: Callable[..., Any]
    # Parameter name -> human description, shown to the policy.
    params: dict[str, str]

    def __call__(self, **kwargs: Any) -> Any:
        return self.fn(**kwargs)


@dataclass
class Decision:
    """What the policy chose to do next."""

    tool: str | None            # None means "stop"
    args: dict[str, Any]
    reasoning: str = ""


class Policy(Protocol):
    """Chooses the next tool call given the goal and what has happened."""

    name: str

    def decide(
        self,
        goal: str,
        tools: dict[str, Tool],
        history: list[ToolCall],
        context: dict[str, Any],
    ) -> Decision: ...


class Agent:
    """Goal-directed, tool-using, budget-bounded, and fully traced."""

    name: str = "agent"

    def __init__(self, policy: Policy, max_steps: int | None = None) -> None:
        self.policy = policy
        self.max_steps = max_steps or SETTINGS.agent_max_steps
        self.tools: dict[str, Tool] = {}
        self.context: dict[str, Any] = {}

    def tool(self, name: str, description: str, **params: str):
        """Register a tool. Used as a decorator inside subclasses."""

        def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = Tool(name, description, fn, params)
            return fn

        return wrap

    def run(self, goal: str, **context: Any) -> AgentRun:
        """Drive the loop until the policy stops or the budget runs out."""
        self.context = dict(context)
        run = AgentRun(agent=self.name, goal=goal, policy=self.policy.name)
        log.info(f"[{self.name}] goal: {goal}  (policy={self.policy.name})")

        for step in range(self.max_steps):
            decision = self.policy.decide(goal, self.tools, run.calls, self.context)

            if decision.tool is None:
                log.debug(f"[{self.name}] policy stopped: {decision.reasoning}")
                break

            tool = self.tools.get(decision.tool)
            if tool is None:
                # A policy that hallucinates a tool name gets a corrective
                # observation rather than a crash, and may try again.
                run.record(
                    ToolCall(
                        tool=decision.tool,
                        args=decision.args,
                        ok=False,
                        observation=f"no such tool; available: {sorted(self.tools)}",
                    )
                )
                continue

            t0 = time.perf_counter()
            try:
                result = tool(**decision.args)
                ok, observation = True, self._describe(result)
                self.context[f"last_{decision.tool}"] = result
            except Exception as exc:  # observations, not crashes
                ok, observation = False, f"{type(exc).__name__}: {exc}"

            elapsed = int((time.perf_counter() - t0) * 1000)
            call = ToolCall(decision.tool, decision.args, ok, observation, elapsed)
            run.record(call)
            marker = "ok " if ok else "ERR"
            log.info(f"[{self.name}] {step + 1}. {marker} {decision.tool} -> {observation[:96]}")

            if self.is_goal_met(self.context):
                break

        succeeded = self.is_goal_met(self.context)
        return run.finish(succeeded, self.summarise(self.context))

    # -- subclass hooks ----------------------------------------------------

    def is_goal_met(self, context: dict[str, Any]) -> bool:
        return False

    def summarise(self, context: dict[str, Any]) -> str:
        return ""

    @staticmethod
    def _describe(result: Any) -> str:
        if result is None:
            return "none"
        if isinstance(result, (list, tuple, set)):
            return f"{len(result)} item(s)"
        if isinstance(result, dict):
            return json.dumps(result, default=str)[:200]
        return str(result)[:200]
