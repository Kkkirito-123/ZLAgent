"""Agent benchmark runner."""

from __future__ import annotations

from harness.agent import AgentOrchestrator, AgentRunResult

from .types import EvalCaseResult, EvalScenario, EvalSuiteResult


class AgentEvalRunner:
    """Run benchmark scenarios through an AgentOrchestrator."""

    def __init__(self, orchestrator: AgentOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def run_scenario(self, scenario: EvalScenario) -> EvalCaseResult:
        result = await self._orchestrator.run(scenario.request)
        return evaluate_agent_result(scenario, result)

    async def run_suite(
        self,
        scenarios: tuple[EvalScenario, ...] | list[EvalScenario],
    ) -> EvalSuiteResult:
        results: list[EvalCaseResult] = []
        for scenario in tuple(scenarios):
            results.append(await self.run_scenario(scenario))
        return EvalSuiteResult(tuple(results))


def evaluate_agent_result(
    scenario: EvalScenario,
    result: AgentRunResult,
) -> EvalCaseResult:
    """Evaluate an agent result against scenario expectations."""

    runtime = result.runtime_result
    failures: list[str] = []
    total_checks = 0
    passed_checks = 0

    def check(condition: bool, failure: str) -> None:
        nonlocal total_checks, passed_checks
        total_checks += 1
        if condition:
            passed_checks += 1
        else:
            failures.append(failure)

    if scenario.expected_accepted is not None:
        check(
            result.accepted is scenario.expected_accepted,
            f"expected accepted={scenario.expected_accepted}, got {result.accepted}",
        )

    if scenario.expected_status is not None:
        check(
            runtime.run.status is scenario.expected_status,
            f"expected status={scenario.expected_status.value}, got {runtime.run.status.value}",
        )

    event_types = tuple(event.type for event in runtime.events)
    for event_type in scenario.required_event_types:
        check(
            event_type in event_types,
            f"missing event type: {event_type.value}",
        )

    checkpoint_statuses = tuple(checkpoint.status for checkpoint in runtime.checkpoints)
    for checkpoint_status in scenario.required_checkpoint_statuses:
        check(
            checkpoint_status in checkpoint_statuses,
            f"missing checkpoint status: {checkpoint_status.value}",
        )

    evidence_refs = _collect_evidence_refs(result)
    for ref in scenario.required_evidence_refs:
        check(ref in evidence_refs, f"missing evidence ref: {ref}")

    tool_failure_count = sum(1 for tool_result in runtime.tool_results if not tool_result.ok)
    if scenario.max_tool_failures is not None:
        check(
            tool_failure_count <= scenario.max_tool_failures,
            (
                "tool failures exceeded limit: "
                f"{tool_failure_count} > {scenario.max_tool_failures}"
            ),
        )

    score = 1.0 if total_checks == 0 else passed_checks / total_checks
    return EvalCaseResult(
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        passed=not failures,
        score=score,
        failures=tuple(failures),
        run_id=runtime.run.id,
        run_status=runtime.run.status,
        accepted=result.accepted,
        event_count=len(runtime.events),
        checkpoint_count=len(runtime.checkpoints),
        tool_failure_count=tool_failure_count,
        evidence_refs=tuple(sorted(evidence_refs)),
    )


def _collect_evidence_refs(result: AgentRunResult) -> set[str]:
    runtime = result.runtime_result
    refs: set[str] = set()
    if runtime.acceptance_decision is not None:
        refs.update(runtime.acceptance_decision.evidence_refs)
    for event in runtime.events:
        refs.update(item.ref for item in event.evidence)
    for tool_result in runtime.tool_results:
        refs.update(item.ref for item in tool_result.evidence)
    return refs
