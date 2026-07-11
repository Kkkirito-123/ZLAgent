"""Agent orchestrator that routes plans through the runtime lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from re_zlagent.harness.runtime import (
    HarnessRuntime,
    RuntimeResult,
    RuntimeSubmission,
)

from .planner import AgentPlanner, AgentRunRequest


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Agent-level run output."""

    request: AgentRunRequest
    runtime_result: RuntimeResult
    planner_metadata: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "planner_metadata", dict(self.planner_metadata))

    @property
    def accepted(self) -> bool:
        return self.runtime_result.accepted


@dataclass(frozen=True, slots=True)
class AgentSubmissionResult:
    """Planned task persisted for later execution by a durable worker."""

    request: AgentRunRequest
    runtime_submission: RuntimeSubmission
    planner_metadata: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "planner_metadata", dict(self.planner_metadata))


class AgentOrchestrator:
    """Thin agent layer.

    The orchestrator owns planning boundaries. The runtime owns tool execution,
    events, checkpoints, and acceptance.
    """

    def __init__(self, *, planner: AgentPlanner, runtime: HarnessRuntime) -> None:
        self._planner = planner
        self._runtime = runtime

    async def submit(self, request: AgentRunRequest) -> AgentSubmissionResult:
        """Plan and persist a task without executing any tool."""

        plan = await self._planner.plan(request)
        submission = self._runtime.submit(
            contract=plan.contract,
            run_id=request.run_id,
            steps=plan.steps,
            model_name=request.model_name,
            prompt_version=request.prompt_version,
            plan_metadata=plan.metadata,
            request_context=request.context,
        )
        return AgentSubmissionResult(
            request=request,
            runtime_submission=submission,
            planner_metadata=plan.metadata,
        )

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        plan = await self._planner.plan(request)
        runtime_result = await self._runtime.run(
            contract=plan.contract,
            run_id=request.run_id,
            steps=plan.steps,
            model_name=request.model_name,
            prompt_version=request.prompt_version,
            plan_metadata=plan.metadata,
            request_context=request.context,
            interactive=request.interactive,
        )
        return AgentRunResult(
            request=request,
            runtime_result=runtime_result,
            planner_metadata=plan.metadata,
        )
