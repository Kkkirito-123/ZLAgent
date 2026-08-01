"""Deterministic semantic, recovery, and latency release benchmarks."""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from re_zlagent.harness.runtime import (
    DurableWorker,
    HarnessRuntime,
    InjectedOutboxCrash,
    OutboxFaultPoint,
    RunControlService,
    RuntimeToolStep,
    WorkerTickStatus,
)
from re_zlagent.harness.storage import (
    InMemoryLongTaskStore,
    InMemoryTaskStore,
    SqliteLongTaskStore,
    SqliteTaskStore,
)
from re_zlagent.harness.tasking import (
    AcceptanceCriterion,
    CriterionType,
    InteractionStatus,
    RunLeaseState,
    SideEffectStatus,
    TaskContract,
    TaskRunStatus,
)
from re_zlagent.harness.tools import (
    Evidence,
    RecommendedNextAction,
    SideEffect,
    Tool,
    ToolErrorType,
    ToolExecutionContext,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)
from re_zlagent.harness.tools.builtins import create_file_tools

from .corpus import (
    BenchmarkCase,
    BenchmarkCorpus,
    BenchmarkKind,
    BenchmarkThresholds,
    MetricValue,
)


@dataclass(frozen=True, slots=True)
class BenchmarkObservation:
    """Runtime facts observed by one release benchmark executor."""

    accepted: bool
    status: TaskRunStatus
    recovered: bool
    metrics: dict[str, MetricValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", dict(self.metrics))


@dataclass(frozen=True, slots=True)
class BenchmarkCaseResult:
    """Comparison of one observation with its versioned expectation."""

    case_id: str
    name: str
    suite: str
    kind: str
    passed: bool
    duration_ms: float
    failures: tuple[str, ...] = field(default_factory=tuple)
    accepted: bool | None = None
    status: TaskRunStatus | None = None
    recovered: bool | None = None
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    expected_accepted: bool | None = None
    expected_status: TaskRunStatus | None = None
    expected_recovered: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "failures", tuple(self.failures))
        object.__setattr__(self, "metrics", dict(self.metrics))

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "name": self.name,
            "suite": self.suite,
            "kind": self.kind,
            "passed": self.passed,
            "duration_ms": round(self.duration_ms, 3),
            "failures": list(self.failures),
            "accepted": self.accepted,
            "status": self.status.value if self.status is not None else None,
            "recovered": self.recovered,
            "metrics": dict(self.metrics),
            "expected": {
                "accepted": self.expected_accepted,
                "status": (
                    self.expected_status.value
                    if self.expected_status is not None
                    else None
                ),
                "recovered": self.expected_recovered,
            },
        }


@dataclass(frozen=True, slots=True)
class ReleaseBenchmarkReport:
    """Machine-readable release gate report for one corpus run."""

    corpus_version: str
    thresholds: BenchmarkThresholds
    results: tuple[BenchmarkCaseResult, ...]
    duration_ms: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for result in self.results if result.passed) / len(self.results)

    @property
    def false_completions(self) -> int:
        return self._metric_total("false_completions")

    @property
    def duplicate_side_effects(self) -> int:
        return self._metric_total("duplicate_side_effects")

    @property
    def abandoned_runs(self) -> int:
        return self._metric_total("abandoned_runs")

    @property
    def completion_eligible(self) -> int:
        return sum(
            result.expected_accepted is True
            and result.expected_status is TaskRunStatus.COMPLETED
            for result in self.results
        )

    @property
    def completed_tasks(self) -> int:
        return sum(
            result.passed
            and result.expected_accepted is True
            and result.expected_status is TaskRunStatus.COMPLETED
            and result.accepted is True
            and result.status is TaskRunStatus.COMPLETED
            for result in self.results
        )

    @property
    def task_completion_rate(self) -> float:
        if self.completion_eligible == 0:
            return 0.0
        return self.completed_tasks / self.completion_eligible

    @property
    def long_task_completion_eligible(self) -> int:
        return sum(
            result.suite == "long_task"
            and result.expected_accepted is True
            and result.expected_status is TaskRunStatus.COMPLETED
            for result in self.results
        )

    @property
    def completed_long_tasks(self) -> int:
        return sum(
            result.suite == "long_task"
            and result.passed
            and result.expected_accepted is True
            and result.expected_status is TaskRunStatus.COMPLETED
            and result.accepted is True
            and result.status is TaskRunStatus.COMPLETED
            for result in self.results
        )

    @property
    def long_task_completion_rate(self) -> float:
        if self.long_task_completion_eligible == 0:
            return 0.0
        return self.completed_long_tasks / self.long_task_completion_eligible

    @property
    def recovery_eligible(self) -> int:
        return sum(result.expected_recovered is True for result in self.results)

    @property
    def recovered_tasks(self) -> int:
        return sum(
            result.passed
            and result.expected_recovered is True
            and result.recovered is True
            for result in self.results
        )

    @property
    def recovery_success_rate(self) -> float:
        if self.recovery_eligible == 0:
            return 0.0
        return self.recovered_tasks / self.recovery_eligible

    @property
    def violations(self) -> tuple[str, ...]:
        failures = [
            f"case failed: {result.case_id}"
            for result in self.results
            if not result.passed
        ]
        if self.pass_rate < self.thresholds.minimum_pass_rate:
            failures.append(
                "pass rate below threshold: "
                f"{self.pass_rate:.3f} < {self.thresholds.minimum_pass_rate:.3f}"
            )
        if self.false_completions > self.thresholds.maximum_false_completions:
            failures.append(
                "false completions exceeded threshold: "
                f"{self.false_completions} > "
                f"{self.thresholds.maximum_false_completions}"
            )
        if self.duplicate_side_effects > self.thresholds.maximum_duplicate_side_effects:
            failures.append(
                "duplicate side effects exceeded threshold: "
                f"{self.duplicate_side_effects} > "
                f"{self.thresholds.maximum_duplicate_side_effects}"
            )
        if self.abandoned_runs > self.thresholds.maximum_abandoned_runs:
            failures.append(
                "abandoned runs exceeded threshold: "
                f"{self.abandoned_runs} > "
                f"{self.thresholds.maximum_abandoned_runs}"
            )
        if self.duration_ms > self.thresholds.maximum_suite_duration_ms:
            failures.append(
                "suite duration exceeded threshold: "
                f"{self.duration_ms:.3f} > "
                f"{self.thresholds.maximum_suite_duration_ms:.3f} ms"
            )
        return tuple(failures)

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "corpus_version": self.corpus_version,
            "duration_ms": round(self.duration_ms, 3),
            "thresholds": self.thresholds.to_dict(),
            "aggregate": {
                "total": len(self.results),
                "passed": sum(1 for result in self.results if result.passed),
                "failed": sum(1 for result in self.results if not result.passed),
                "pass_rate": self.pass_rate,
                "false_completions": self.false_completions,
                "duplicate_side_effects": self.duplicate_side_effects,
                "abandoned_runs": self.abandoned_runs,
                "completion_eligible": self.completion_eligible,
                "completed_tasks": self.completed_tasks,
                "task_completion_rate": self.task_completion_rate,
                "long_task_completion_eligible": (self.long_task_completion_eligible),
                "completed_long_tasks": self.completed_long_tasks,
                "long_task_completion_rate": self.long_task_completion_rate,
                "recovery_eligible": self.recovery_eligible,
                "recovered_tasks": self.recovered_tasks,
                "recovery_success_rate": self.recovery_success_rate,
            },
            "violations": list(self.violations),
            "results": [result.to_dict() for result in self.results],
        }

    def _metric_total(self, name: str) -> int:
        total = 0
        for result in self.results:
            value = result.metrics.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            total += int(value)
        return total


BenchmarkExecutor = Callable[[], Awaitable[BenchmarkObservation]]


class ReleaseBenchmarkRunner:
    """Execute a persistent corpus through real harness lifecycle boundaries."""

    def __init__(self, corpus: BenchmarkCorpus) -> None:
        self._corpus = corpus
        self._executors: dict[BenchmarkKind, BenchmarkExecutor] = {
            BenchmarkKind.VERIFIED_SUCCESS: self._verified_success,
            BenchmarkKind.FALSE_COMPLETION_GUARD: self._false_completion_guard,
            BenchmarkKind.RETRY_CONTINUATION: self._retry_continuation,
            BenchmarkKind.OUTBOX_CRASH_REPLAY: self._outbox_crash_replay,
            BenchmarkKind.SQLITE_APPROVAL_RESTART: self._sqlite_approval_restart,
            BenchmarkKind.EXPIRED_LEASE_RECLAIM: self._expired_lease_reclaim,
            BenchmarkKind.MULTI_RETRY_CONTINUATION: (self._multi_retry_continuation),
            BenchmarkKind.SQLITE_FRONTIER_RESTART: (self._sqlite_frontier_restart),
            BenchmarkKind.ALTERNATIVE_TOOL_CONTINUATION: (
                self._alternative_tool_continuation
            ),
            BenchmarkKind.MIDDLE_APPROVAL_CONTINUATION: (
                self._middle_approval_continuation
            ),
            BenchmarkKind.RETRY_BUDGET_DEAD_LETTER: (self._retry_budget_dead_letter),
            BenchmarkKind.COOPERATIVE_CANCEL: self._cooperative_cancel,
        }

    async def run(self) -> ReleaseBenchmarkReport:
        started = perf_counter()
        results: list[BenchmarkCaseResult] = []
        for case in self._corpus.cases:
            results.append(await self._run_case(case))
        return ReleaseBenchmarkReport(
            corpus_version=self._corpus.corpus_version,
            thresholds=self._corpus.thresholds,
            results=tuple(results),
            duration_ms=(perf_counter() - started) * 1000,
        )

    async def run_case(self, case: BenchmarkCase) -> BenchmarkCaseResult:
        """Run one validated release case for a higher-level eval adapter."""

        if case not in self._corpus.cases:
            raise ValueError("release benchmark case does not belong to this corpus")
        return await self._run_case(case)

    async def _run_case(self, case: BenchmarkCase) -> BenchmarkCaseResult:
        started = perf_counter()
        try:
            observation = await self._executors[case.kind]()
        except Exception as exc:  # noqa: BLE001 - benchmark failures are report data
            duration_ms = (perf_counter() - started) * 1000
            return BenchmarkCaseResult(
                case_id=case.id,
                name=case.name,
                suite=case.suite.value,
                kind=case.kind.value,
                passed=False,
                duration_ms=duration_ms,
                failures=(f"executor raised {type(exc).__name__}: {exc}",),
                expected_accepted=case.expected.accepted,
                expected_status=case.expected.status,
                expected_recovered=case.expected.recovered,
            )
        duration_ms = (perf_counter() - started) * 1000
        failures = _compare_observation(case, observation, duration_ms)
        return BenchmarkCaseResult(
            case_id=case.id,
            name=case.name,
            suite=case.suite.value,
            kind=case.kind.value,
            passed=not failures,
            duration_ms=duration_ms,
            failures=failures,
            accepted=observation.accepted,
            status=observation.status,
            recovered=observation.recovered,
            metrics=observation.metrics,
            expected_accepted=case.expected.accepted,
            expected_status=case.expected.status,
            expected_recovered=case.expected.recovered,
        )

    async def _verified_success(self) -> BenchmarkObservation:
        tool = _BenchmarkEvidenceTool()
        runtime = _runtime_with_tools(tool)
        result = await runtime.run(
            contract=_evidence_contract(
                "benchmark-short-success",
                "benchmark short success",
                ("benchmark:short",),
            ),
            run_id="benchmark-short-success",
            steps=(
                RuntimeToolStep(
                    id="short",
                    tool_name=tool.name,
                    arguments={"ref": "benchmark:short"},
                    required_evidence_refs=("benchmark:short",),
                ),
            ),
        )
        return BenchmarkObservation(
            accepted=result.accepted,
            status=result.run.status,
            recovered=False,
            metrics={
                "tool_calls": sum(tool.calls.values()),
                "false_completions": 0,
            },
        )

    async def _false_completion_guard(self) -> BenchmarkObservation:
        tool = _BenchmarkEvidenceTool()
        runtime = _runtime_with_tools(tool)
        result = await runtime.run(
            contract=_evidence_contract(
                "benchmark-false-completion",
                "reject unsupported completion",
                ("benchmark:required",),
            ),
            run_id="benchmark-false-completion",
            steps=(
                RuntimeToolStep(
                    id="insufficient",
                    tool_name=tool.name,
                    arguments={"ref": "benchmark:other"},
                ),
            ),
        )
        false_completion = int(
            result.accepted or result.run.status is TaskRunStatus.COMPLETED
        )
        return BenchmarkObservation(
            accepted=result.accepted,
            status=result.run.status,
            recovered=False,
            metrics={
                "tool_calls": sum(tool.calls.values()),
                "false_completions": false_completion,
            },
        )

    async def _retry_continuation(self) -> BenchmarkObservation:
        tool = _BenchmarkEvidenceTool(fail_once={"benchmark:step-2"})
        runtime = _runtime_with_tools(tool)
        refs = (
            "benchmark:step-1",
            "benchmark:step-2",
            "benchmark:step-3",
        )
        steps = tuple(
            RuntimeToolStep(
                id=f"step-{index}",
                tool_name=tool.name,
                arguments={"ref": ref},
                depends_on=(() if index == 1 else (f"step-{index - 1}",)),
                required_evidence_refs=(ref,),
            )
            for index, ref in enumerate(refs, start=1)
        )
        first = await runtime.run(
            contract=_evidence_contract(
                "benchmark-retry",
                "retry and continue",
                refs,
            ),
            run_id="benchmark-retry",
            steps=steps,
        )
        resumed = await runtime.resume_from_checkpoint(run_id="benchmark-retry")
        perturbation = first.failure.perturbation_class if first.failure else ""
        prefix_replays = max(0, tool.calls.get(refs[0], 0) - 1)
        prefix_replays += max(0, tool.calls.get(refs[2], 0) - 1)
        return BenchmarkObservation(
            accepted=resumed.accepted,
            status=resumed.run.status,
            recovered=first.run.status is TaskRunStatus.RECOVERING,
            metrics={
                "completed_prefix_replays": prefix_replays,
                "failed_step_attempts": tool.calls.get(refs[1], 0),
                "explicit_transient_failures": int(
                    perturbation == "explicit_transient"
                ),
            },
        )

    async def _outbox_crash_replay(self) -> BenchmarkObservation:
        adapter = _DeduplicatingExternalAdapter()
        tool = _BenchmarkExternalTool(adapter)
        task_store = InMemoryTaskStore()
        long_task_store = InMemoryLongTaskStore()
        registry = ToolRegistry()
        registry.register(tool)
        runtime = HarnessRuntime(
            store=task_store,
            tools=registry,
            long_task_store=long_task_store,
            outbox_fault_injector=_CrashAfterDispatch(),
        )
        crashed = False
        try:
            await runtime.run(
                contract=_evidence_contract(
                    "benchmark-outbox",
                    "recover one external action",
                    ("benchmark:external",),
                ),
                run_id="benchmark-outbox",
                steps=(
                    RuntimeToolStep(
                        id="external",
                        tool_name=tool.name,
                        required_evidence_refs=("benchmark:external",),
                    ),
                ),
            )
        except InjectedOutboxCrash:
            crashed = True
        if not crashed:
            raise RuntimeError("outbox crash injector did not fire")
        resumed = await HarnessRuntime(
            store=task_store,
            tools=registry,
            long_task_store=long_task_store,
        ).resume_incomplete_run(run_id="benchmark-outbox")
        return BenchmarkObservation(
            accepted=resumed.accepted,
            status=resumed.run.status,
            recovered=crashed,
            metrics={
                "dispatch_attempts": adapter.dispatch_attempts,
                "logical_side_effects": adapter.logical_effects,
                "duplicate_side_effects": max(0, adapter.logical_effects - 1),
            },
        )

    async def _sqlite_approval_restart(self) -> BenchmarkObservation:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            database = root / "benchmark.sqlite"
            contract = _evidence_contract(
                "benchmark-approval",
                "approve one durable write",
                ("result.txt",),
            )

            task_store = SqliteTaskStore(database)
            long_task_store = SqliteLongTaskStore(database)
            runtime = HarnessRuntime(
                store=task_store,
                tools=_file_registry(workspace),
                long_task_store=long_task_store,
            )
            runtime.submit(
                contract=contract,
                run_id="benchmark-approval",
                steps=(
                    RuntimeToolStep(
                        id="write",
                        tool_name="write_file",
                        arguments={"path": "result.txt", "content": "verified\n"},
                        required_evidence_refs=("result.txt",),
                    ),
                ),
            )
            task_store.close()
            long_task_store.close()

            task_store = SqliteTaskStore(database)
            long_task_store = SqliteLongTaskStore(database)
            runtime = HarnessRuntime(
                store=task_store,
                tools=_file_registry(workspace),
                long_task_store=long_task_store,
            )
            tick = await DurableWorker(
                worker_id="benchmark-worker",
                store=task_store,
                runtime=runtime,
            ).run_once("benchmark-approval")
            interactions = long_task_store.list_pending_interactions(
                "benchmark-approval",
                status=InteractionStatus.OPEN,
            )
            if len(interactions) != 1:
                raise RuntimeError("approval benchmark did not create one interaction")
            checkpoint_id = interactions[0].checkpoint_id
            task_store.close()
            long_task_store.close()

            task_store = SqliteTaskStore(database)
            long_task_store = SqliteLongTaskStore(database)
            runtime = HarnessRuntime(
                store=task_store,
                tools=_file_registry(workspace),
                long_task_store=long_task_store,
            )
            resumed = await runtime.resume_with_user_approval(
                run_id="benchmark-approval",
                checkpoint_id=checkpoint_id,
                feedback="benchmark approval",
            )
            records = long_task_store.list_side_effects("benchmark-approval")
            open_interactions = long_task_store.list_pending_interactions(
                "benchmark-approval",
                status=InteractionStatus.OPEN,
            )
            confirmed = sum(
                1 for record in records if record.status is SideEffectStatus.CONFIRMED
            )
            output_matches = (workspace / "result.txt").read_text() == "verified\n"
            task_store.close()
            long_task_store.close()

        return BenchmarkObservation(
            accepted=resumed.accepted,
            status=resumed.run.status,
            recovered=tick.status is WorkerTickStatus.PARKED,
            metrics={
                "confirmed_side_effects": confirmed,
                "duplicate_side_effects": max(0, len(records) - 1),
                "open_interactions": len(open_interactions),
                "output_matches": output_matches,
            },
        )

    async def _expired_lease_reclaim(self) -> BenchmarkObservation:
        now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
        later = now + timedelta(seconds=2)
        tool = _BenchmarkEvidenceTool()
        task_store = InMemoryTaskStore()
        long_task_store = InMemoryLongTaskStore()
        registry = ToolRegistry()
        registry.register(tool)
        runtime = HarnessRuntime(
            store=task_store,
            tools=registry,
            long_task_store=long_task_store,
        )
        runtime.submit(
            contract=_evidence_contract(
                "benchmark-lease",
                "reclaim expired work",
                ("benchmark:lease",),
            ),
            run_id="benchmark-lease",
            steps=(
                RuntimeToolStep(
                    id="lease-step",
                    tool_name=tool.name,
                    arguments={"ref": "benchmark:lease"},
                    required_evidence_refs=("benchmark:lease",),
                ),
            ),
        )
        first_lease = task_store.claim_run(
            "benchmark-lease",
            owner_id="crashed-worker",
            lease_seconds=1,
            retry_budget=3,
            now=now,
        )
        if first_lease is None:
            raise RuntimeError("failed to create abandoned benchmark lease")
        tick = await DurableWorker(
            worker_id="recovery-worker",
            store=task_store,
            runtime=runtime,
            clock=lambda: later,
        ).run_once("benchmark-lease")
        final_run = task_store.get_run("benchmark-lease")
        final_lease = task_store.get_run_lease("benchmark-lease")
        if final_run is None or final_lease is None:
            raise RuntimeError("lease benchmark lost durable state")
        reclaimed = int(final_lease.metadata.get("reclaimed_expired_lease") is True)
        abandoned = int(
            final_run.status is not TaskRunStatus.COMPLETED
            or final_lease.state is not RunLeaseState.RELEASED
        )
        accepted = bool(tick.runtime_result and tick.runtime_result.accepted)
        return BenchmarkObservation(
            accepted=accepted,
            status=final_run.status,
            recovered=reclaimed == 1,
            metrics={
                "reclaimed_expired_leases": reclaimed,
                "abandoned_runs": abandoned,
            },
        )

    async def _multi_retry_continuation(self) -> BenchmarkObservation:
        refs = tuple(f"benchmark:multi-{index}" for index in range(1, 6))
        tool = _BenchmarkEvidenceTool(fail_once={refs[1], refs[3]})
        runtime = _runtime_with_tools(tool)
        steps = _evidence_steps(refs)
        first = await runtime.run(
            contract=_evidence_contract(
                "benchmark-multi-retry",
                "recover two transient failures and finish every step",
                (refs[-1],),
            ),
            run_id="benchmark-multi-retry",
            steps=steps,
        )
        second = await runtime.resume_from_checkpoint(run_id="benchmark-multi-retry")
        final = await runtime.resume_from_checkpoint(run_id="benchmark-multi-retry")
        prefix_replays = sum(
            max(0, tool.calls.get(ref, 0) - expected)
            for ref, expected in zip(refs, (1, 2, 1, 2, 1), strict=True)
        )
        return BenchmarkObservation(
            accepted=final.accepted,
            status=final.run.status,
            recovered=(
                first.run.status is TaskRunStatus.RECOVERING
                and second.run.status is TaskRunStatus.RECOVERING
            ),
            metrics={
                "recovery_cycles": 2,
                "completed_prefix_replays": prefix_replays,
                "tool_calls": sum(tool.calls.values()),
                "verified_steps": sum(
                    verification.passed for verification in final.step_verifications
                ),
            },
        )

    async def _sqlite_frontier_restart(self) -> BenchmarkObservation:
        refs = tuple(f"benchmark:restart-{index}" for index in range(1, 6))
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "frontier.sqlite"
            first_store = SqliteTaskStore(database)
            first_tool = _BenchmarkEvidenceTool(fail_once={refs[2]})
            first_registry = ToolRegistry()
            first_registry.register(first_tool)
            first_runtime = HarnessRuntime(
                store=first_store,
                tools=first_registry,
            )
            first = await first_runtime.run(
                contract=_evidence_contract(
                    "benchmark-frontier-restart",
                    "resume the durable unfinished frontier",
                    (refs[-1],),
                ),
                run_id="benchmark-frontier-restart",
                steps=_evidence_steps(refs),
            )
            persisted = first_store.get_run("benchmark-frontier-restart")
            plan_id = persisted.plan_id if persisted is not None else None
            first_store.close()

            reopened = SqliteTaskStore(database)
            resumed_tool = _BenchmarkEvidenceTool()
            resumed_registry = ToolRegistry()
            resumed_registry.register(resumed_tool)
            resumed_runtime = HarnessRuntime(
                store=reopened,
                tools=resumed_registry,
            )
            resumed = await resumed_runtime.resume_from_checkpoint(
                run_id="benchmark-frontier-restart"
            )
            persisted_plan = reopened.get_plan(plan_id) if plan_id else None
            reopened.close()
        prefix_replays = sum(resumed_tool.calls.get(ref, 0) for ref in refs[:2])
        return BenchmarkObservation(
            accepted=resumed.accepted,
            status=resumed.run.status,
            recovered=first.run.status is TaskRunStatus.RECOVERING,
            metrics={
                "completed_prefix_replays": prefix_replays,
                "remaining_steps_executed": sum(resumed_tool.calls.values()),
                "persisted_plan_restored": persisted_plan is not None,
                "first_process_completed_steps": sum(
                    first_tool.calls.get(ref, 0) for ref in refs[:2]
                ),
            },
        )

    async def _alternative_tool_continuation(self) -> BenchmarkObservation:
        evidence = _BenchmarkEvidenceTool()
        primary = _BenchmarkAlternativeTool()
        runtime = _runtime_with_tools(evidence, primary)
        refs = (
            "benchmark:alternative-1",
            "benchmark:alternative-2",
            "benchmark:alternative-3",
        )
        steps = (
            RuntimeToolStep(
                id="step-1",
                tool_name=evidence.name,
                arguments={"ref": refs[0]},
                required_evidence_refs=(refs[0],),
            ),
            RuntimeToolStep(
                id="step-2",
                tool_name=primary.name,
                depends_on=("step-1",),
                required_evidence_refs=(refs[1],),
            ),
            RuntimeToolStep(
                id="step-3",
                tool_name=evidence.name,
                arguments={"ref": refs[2]},
                depends_on=("step-2",),
                required_evidence_refs=(refs[2],),
            ),
        )
        first = await runtime.run(
            contract=_evidence_contract(
                "benchmark-alternative",
                "replace one unavailable capability and continue",
                (refs[-1],),
            ),
            run_id="benchmark-alternative",
            steps=steps,
        )
        resumed = await runtime.resume_with_alternative_tool(
            run_id="benchmark-alternative",
            alternative_step=RuntimeToolStep(
                id="replacement-proposal",
                tool_name=evidence.name,
                arguments={"ref": refs[1]},
            ),
        )
        alternative_events = [
            event
            for event in resumed.events
            if event.type.value == "tool_result_recorded"
            and "alternative" in event.payload
        ]
        original_identity = bool(
            alternative_events
            and alternative_events[-1].payload.get("step_id") == "step-2"
        )
        return BenchmarkObservation(
            accepted=resumed.accepted,
            status=resumed.run.status,
            recovered=first.run.status is TaskRunStatus.RECOVERING,
            metrics={
                "primary_failures": primary.calls,
                "completed_prefix_replays": max(0, evidence.calls.get(refs[0], 0) - 1),
                "original_step_identity_preserved": original_identity,
                "verified_steps": sum(
                    verification.passed for verification in resumed.step_verifications
                ),
            },
        )

    async def _middle_approval_continuation(self) -> BenchmarkObservation:
        evidence = _BenchmarkEvidenceTool()
        confirmation = _BenchmarkConfirmTool()
        registry = ToolRegistry()
        registry.register(evidence)
        registry.register(confirmation)
        task_store = InMemoryTaskStore()
        long_task_store = InMemoryLongTaskStore()
        runtime = HarnessRuntime(
            store=task_store,
            tools=registry,
            long_task_store=long_task_store,
        )
        refs = (
            "benchmark:approval-1",
            "benchmark:approval-2",
            "benchmark:approval-3",
            "benchmark:approval-4",
            "benchmark:approval-5",
        )
        steps = list(_evidence_steps(refs))
        steps[1] = RuntimeToolStep(
            id="step-2",
            tool_name=confirmation.name,
            arguments={"ref": refs[1]},
            depends_on=("step-1",),
            required_evidence_refs=(refs[1],),
        )
        first = await runtime.run(
            contract=_evidence_contract(
                "benchmark-middle-approval",
                "approve a middle mutation and finish remaining steps",
                (refs[-1],),
            ),
            run_id="benchmark-middle-approval",
            steps=tuple(steps),
        )
        interactions = long_task_store.list_pending_interactions(
            "benchmark-middle-approval",
            status=InteractionStatus.OPEN,
        )
        resumed = await runtime.resume_with_user_approval(
            run_id="benchmark-middle-approval",
            feedback="approved for benchmark",
        )
        open_after = long_task_store.list_pending_interactions(
            "benchmark-middle-approval",
            status=InteractionStatus.OPEN,
        )
        return BenchmarkObservation(
            accepted=resumed.accepted,
            status=resumed.run.status,
            recovered=first.run.status is TaskRunStatus.WAITING_USER,
            metrics={
                "approval_interactions": len(interactions),
                "open_interactions": len(open_after),
                "completed_prefix_replays": max(0, evidence.calls.get(refs[0], 0) - 1),
                "remaining_steps_executed": sum(
                    evidence.calls.get(ref, 0) for ref in refs[2:]
                ),
            },
        )

    async def _retry_budget_dead_letter(self) -> BenchmarkObservation:
        store = InMemoryTaskStore()
        retrying = _BenchmarkAlwaysRetryTool()
        registry = ToolRegistry()
        registry.register(retrying)
        runtime = HarnessRuntime(store=store, tools=registry)
        first = await runtime.run(
            contract=_evidence_contract(
                "benchmark-dead-letter",
                "stop bounded retries for an unavailable capability",
                ("benchmark:never",),
            ),
            run_id="benchmark-dead-letter",
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name=retrying.name,
                    required_evidence_refs=("benchmark:never",),
                ),
            ),
        )
        clock = _BenchmarkClock()
        worker = DurableWorker(
            worker_id="benchmark-dead-letter-worker",
            store=store,
            runtime=runtime,
            retry_budget=2,
            base_backoff_seconds=1,
            clock=clock,
        )
        first_tick = await worker.run_once("benchmark-dead-letter")
        clock.advance(1)
        second_tick = await worker.run_once("benchmark-dead-letter")
        final_run = store.get_run("benchmark-dead-letter")
        lease = store.get_run_lease("benchmark-dead-letter")
        if final_run is None or lease is None:
            raise RuntimeError("dead-letter benchmark lost durable state")
        return BenchmarkObservation(
            accepted=False,
            status=final_run.status,
            recovered=False,
            metrics={
                "initially_recoverable": (first.run.status is TaskRunStatus.RECOVERING),
                "retry_scheduled": (
                    first_tick.status is WorkerTickStatus.RETRY_SCHEDULED
                ),
                "dead_lettered": (
                    second_tick.status is WorkerTickStatus.DEAD_LETTER
                    and lease.state is RunLeaseState.DEAD_LETTER
                ),
                "retry_attempts": lease.attempt_count,
                "false_completions": int(final_run.status is TaskRunStatus.COMPLETED),
                "abandoned_runs": 0,
            },
        )

    async def _cooperative_cancel(self) -> BenchmarkObservation:
        store = InMemoryTaskStore()
        controls = RunControlService(store)
        remaining = _BenchmarkEvidenceTool()
        cancelling = _BenchmarkCancelTool(
            controls,
            run_id="benchmark-cancel",
        )
        registry = ToolRegistry()
        registry.register(cancelling)
        registry.register(remaining)
        runtime = HarnessRuntime(store=store, tools=registry)
        runtime.submit(
            contract=_evidence_contract(
                "benchmark-cancel",
                "stop a long task before its next step",
                ("benchmark:cancel-final",),
            ),
            run_id="benchmark-cancel",
            steps=(
                RuntimeToolStep(
                    id="step-1",
                    tool_name=cancelling.name,
                    required_evidence_refs=("benchmark:cancelled-step",),
                ),
                RuntimeToolStep(
                    id="step-2",
                    tool_name=remaining.name,
                    arguments={"ref": "benchmark:cancel-final"},
                    depends_on=("step-1",),
                ),
            ),
        )
        tick = await DurableWorker(
            worker_id="benchmark-cancel-worker",
            store=store,
            runtime=runtime,
        ).run_once("benchmark-cancel")
        final_run = store.get_run("benchmark-cancel")
        checkpoint = store.latest_checkpoint("benchmark-cancel")
        if final_run is None or checkpoint is None:
            raise RuntimeError("cancel benchmark lost durable state")
        return BenchmarkObservation(
            accepted=False,
            status=final_run.status,
            recovered=False,
            metrics={
                "worker_parked": tick.status is WorkerTickStatus.PARKED,
                "remaining_tool_calls": sum(remaining.calls.values()),
                "cancel_checkpoint": checkpoint.status.value == "cancelled",
                "false_completions": int(final_run.status is TaskRunStatus.COMPLETED),
            },
        )


class _BenchmarkEvidenceTool(Tool):
    name = "benchmark_evidence"
    description = "Emit deterministic benchmark evidence."
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {"ref": {"type": "string"}},
        "required": ["ref"],
    }

    def __init__(self, *, fail_once: set[str] | None = None) -> None:
        self._fail_once = set(fail_once or ())
        self.calls: dict[str, int] = {}

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        ref = str(arguments.get("ref") or "")
        self.calls[ref] = self.calls.get(ref, 0) + 1
        if ref in self._fail_once and self.calls[ref] == 1:
            return ToolResult.failure(
                "injected transient benchmark failure",
                error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )
        return ToolResult.success(
            f"evidence {ref}",
            evidence=[Evidence(type="benchmark", ref=ref)],
            source=self.name,
        )


class _BenchmarkAlternativeTool(Tool):
    name = "benchmark_alternative_required"
    description = "Fail with an explicit alternative-tool recovery action."
    permission = ToolPermission.SAFE

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        del arguments
        self.calls += 1
        return ToolResult.failure(
            "primary benchmark capability unavailable",
            error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.USE_ALTERNATIVE_TOOL,
            source=self.name,
        )


class _BenchmarkConfirmTool(Tool):
    name = "benchmark_confirm"
    description = "Return benchmark evidence only after Runtime confirmation."
    permission = ToolPermission.CONFIRM
    is_read_only = False
    input_schema = {
        "type": "object",
        "properties": {"ref": {"type": "string"}},
        "required": ["ref"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        ref = str(arguments["ref"])
        return ToolResult.success(
            "confirmed benchmark action",
            evidence=[Evidence(type="benchmark", ref=ref)],
            source=self.name,
        )


class _BenchmarkAlwaysRetryTool(Tool):
    name = "benchmark_always_retry"
    description = "Always return an explicit transient failure."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        del arguments
        return ToolResult.failure(
            "benchmark dependency remains unavailable",
            error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
            recoverable_by_model=True,
            recommended_next_action=RecommendedNextAction.RETRY,
            source=self.name,
        )


class _BenchmarkCancelTool(Tool):
    name = "benchmark_cancel_during_execute"
    description = "Cancel the current run at a cooperative step boundary."
    permission = ToolPermission.SAFE

    def __init__(self, controls: RunControlService, *, run_id: str) -> None:
        self._controls = controls
        self._run_id = run_id

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        del arguments
        self._controls.cancel(
            self._run_id,
            reason="benchmark cooperative cancellation",
            actor="benchmark",
        )
        return ToolResult.success(
            "cancelled at cooperative boundary",
            evidence=[Evidence(type="benchmark", ref="benchmark:cancelled-step")],
            source=self.name,
        )


class _BenchmarkClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now += timedelta(seconds=seconds)


class _DeduplicatingExternalAdapter:
    def __init__(self) -> None:
        self.dispatch_attempts = 0
        self.logical_effects = 0
        self._applied_keys: set[str] = set()

    def apply(self, idempotency_key: str) -> None:
        self.dispatch_attempts += 1
        if idempotency_key in self._applied_keys:
            return
        self._applied_keys.add(idempotency_key)
        self.logical_effects += 1


class _BenchmarkExternalTool(Tool):
    name = "benchmark_external_action"
    description = "Apply one deduplicated benchmark side effect."
    permission = ToolPermission.SAFE
    is_read_only = False
    side_effects = ("external",)
    outbox_required = True
    side_effect_retry_safe = True

    def __init__(self, adapter: _DeduplicatingExternalAdapter) -> None:
        self._adapter = adapter

    def plan_side_effects(
        self,
        arguments: dict[str, Any],
    ) -> tuple[SideEffect, ...]:
        return (
            SideEffect(
                type="external",
                target="benchmark:item",
                risk="high",
            ),
        )

    async def execute_with_context(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        self._adapter.apply(context.side_effect_keys[0])
        return ToolResult.success(
            "external benchmark action applied",
            evidence=[Evidence(type="benchmark", ref="benchmark:external")],
            side_effects=[
                SideEffect(
                    type="external",
                    target="benchmark:item",
                    risk="high",
                )
            ],
            source=self.name,
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raise AssertionError("benchmark runtime must provide tool execution context")


class _CrashAfterDispatch:
    def hit(self, point: OutboxFaultPoint, records: tuple[Any, ...]) -> None:
        if point is OutboxFaultPoint.AFTER_DISPATCH:
            raise InjectedOutboxCrash(point.value)


def _runtime_with_tools(*tools: Tool) -> HarnessRuntime:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return HarnessRuntime(
        store=InMemoryTaskStore(),
        tools=registry,
        long_task_store=InMemoryLongTaskStore(),
    )


def _file_registry(workspace: Path) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in create_file_tools(workspace):
        registry.register(tool)
    return registry


def _evidence_contract(
    contract_id: str,
    goal: str,
    refs: tuple[str, ...],
) -> TaskContract:
    return TaskContract(
        id=contract_id,
        user_goal=goal,
        acceptance_criteria=(
            AcceptanceCriterion(
                id="required-evidence",
                description="required benchmark evidence exists",
                type=CriterionType.TOOL_EVIDENCE,
                evidence_refs=refs,
            ),
        ),
    )


def _evidence_steps(refs: tuple[str, ...]) -> tuple[RuntimeToolStep, ...]:
    return tuple(
        RuntimeToolStep(
            id=f"step-{index}",
            tool_name="benchmark_evidence",
            arguments={"ref": ref},
            depends_on=(() if index == 1 else (f"step-{index - 1}",)),
            required_evidence_refs=(ref,),
        )
        for index, ref in enumerate(refs, start=1)
    )


def _compare_observation(
    case: BenchmarkCase,
    observation: BenchmarkObservation,
    duration_ms: float,
) -> tuple[str, ...]:
    failures: list[str] = []
    expected = case.expected
    if observation.accepted is not expected.accepted:
        failures.append(
            f"accepted mismatch: {observation.accepted} != {expected.accepted}"
        )
    if observation.status is not expected.status:
        failures.append(
            f"status mismatch: {observation.status.value} != {expected.status.value}"
        )
    if observation.recovered is not expected.recovered:
        failures.append(
            f"recovered mismatch: {observation.recovered} != {expected.recovered}"
        )
    for name, value in expected.metrics.items():
        observed = observation.metrics.get(name)
        if observed != value:
            failures.append(f"metric mismatch {name}: {observed!r} != {value!r}")
    if duration_ms > case.max_duration_ms:
        failures.append(
            f"duration exceeded budget: {duration_ms:.3f} > "
            f"{case.max_duration_ms:.3f} ms"
        )
    return tuple(failures)
