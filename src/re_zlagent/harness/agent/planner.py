"""Planner boundary for harness agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from re_zlagent.harness.runtime import RuntimeToolStep
from re_zlagent.harness.tasking import PlanDAG, TaskContract


@dataclass(frozen=True, slots=True)
class AgentRunRequest:
    """Input to the planner/orchestrator layer."""

    run_id: str
    user_goal: str
    context: dict[str, Any] = field(default_factory=dict)
    model_name: str | None = None
    prompt_version: str | None = None
    interactive: bool = True

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("request.run_id must be non-empty")
        if not self.user_goal.strip():
            raise ValueError("request.user_goal must be non-empty")
        object.__setattr__(self, "context", dict(self.context))


@dataclass(frozen=True, slots=True)
class AgentPlan:
    """Planner output consumed by the runtime lifecycle."""

    contract: TaskContract
    steps: tuple[RuntimeToolStep, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "metadata", dict(self.metadata))
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate runtime step id: {step.id}")
            seen.add(step.id)
        dag = PlanDAG(tuple(step.to_plan_step() for step in self.steps))
        dag.validate_linear_order(tuple(step.id for step in self.steps))


class AgentPlanner(Protocol):
    """Planner interface.

    Future LLM planners should implement this boundary. They should produce
    contracts and runtime steps, not execute tools directly.
    """

    async def plan(self, request: AgentRunRequest) -> AgentPlan:
        """Return a deterministic plan for a request."""


class StaticAgentPlanner:
    """Test and bootstrap planner that returns a fixed plan."""

    def __init__(self, plan: AgentPlan) -> None:
        self._plan = plan

    async def plan(self, request: AgentRunRequest) -> AgentPlan:
        return self._plan
