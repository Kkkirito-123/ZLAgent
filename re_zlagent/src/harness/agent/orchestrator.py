"""Agent orchestrator that routes plans through the runtime lifecycle."""

from __future__ import annotations

from dataclasses import dataclass

from harness.runtime import HarnessRuntime, RuntimeResult

from .planner import AgentPlanner, AgentRunRequest


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Agent-level run output."""

    request: AgentRunRequest
    runtime_result: RuntimeResult
    planner_metadata: dict

    @property
    def accepted(self) -> bool:
        return self.runtime_result.accepted


class AgentOrchestrator:
    """Thin agent layer.

    The orchestrator owns planning boundaries. The runtime owns tool execution,
    events, checkpoints, and acceptance.
    """

    def __init__(self, *, planner: AgentPlanner, runtime: HarnessRuntime) -> None:
        self._planner = planner
        self._runtime = runtime

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        plan = await self._planner.plan(request)
        runtime_result = await self._runtime.run(
            contract=plan.contract,
            run_id=request.run_id,
            steps=plan.steps,
            acceptance=plan.acceptance,
            model_name=request.model_name,
            prompt_version=request.prompt_version,
            interactive=request.interactive,
        )
        return AgentRunResult(
            request=request,
            runtime_result=runtime_result,
            planner_metadata=plan.metadata,
        )
