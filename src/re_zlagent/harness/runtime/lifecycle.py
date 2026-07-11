"""Minimal harness run lifecycle.

This module intentionally does not call an LLM. It executes deterministic
runtime steps so the project can verify run state, tool boundaries, events,
checkpoints, and acceptance before adding model planning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from re_zlagent.harness.storage import LongTaskStore, TaskStore
from re_zlagent.harness.tasking import (
    AcceptanceDecision,
    AcceptanceGate,
    AcceptanceStatus,
    Checkpoint,
    CheckpointStatus,
    CriterionType,
    FailureDuration,
    FailureEnvelope,
    FailureType,
    FailureVisibility,
    InteractionKind,
    PendingInteraction,
    PlanDAG,
    PlanStep,
    ProgramPlan,
    RecoveryAction,
    RecoveryPolicy,
    ResumePolicy,
    StepStatus,
    StepVerification,
    StepVerifier,
    TaskContract,
    TaskEvent,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import ToolRegistry, ToolResult

from .context_pack import ContextPack, ContextPackBuilder
from .outbox import (
    OutboxAction,
    OutboxFaultInjector,
    SideEffectOutbox,
)


@dataclass(frozen=True, slots=True)
class RuntimeToolStep:
    """One deterministic tool step in a run."""

    id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    allow_confirm: bool = False
    title: str | None = None
    expected_output: str = ""
    verification: str = ""
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    required_evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("step.id must be non-empty")
        if not self.tool_name.strip():
            raise ValueError("step.tool_name must be non-empty")
        object.__setattr__(self, "arguments", dict(self.arguments))
        object.__setattr__(
            self,
            "depends_on",
            tuple(str(item) for item in self.depends_on),
        )
        object.__setattr__(
            self,
            "required_evidence_refs",
            tuple(str(item) for item in self.required_evidence_refs),
        )

    def to_plan_step(self) -> PlanStep:
        return PlanStep(
            id=self.id,
            title=self.title or self.id,
            expected_output=self.expected_output,
            verification=self.verification,
            depends_on=self.depends_on,
            required_evidence_refs=self.required_evidence_refs,
            metadata={
                "tool_name": self.tool_name,
                "tool_arguments": dict(self.arguments),
                "allow_confirm": self.allow_confirm,
            },
        )

    @classmethod
    def from_plan_step(cls, step: PlanStep) -> "RuntimeToolStep":
        """Rebuild an executable step from a persisted plan step."""

        metadata = dict(step.metadata)
        return cls(
            id=step.id,
            tool_name=str(metadata.get("tool_name") or ""),
            arguments=dict(metadata.get("tool_arguments") or {}),
            allow_confirm=bool(metadata.get("allow_confirm", False)),
            title=step.title,
            expected_output=step.expected_output,
            verification=step.verification,
            depends_on=step.depends_on,
            required_evidence_refs=step.required_evidence_refs,
        )


@dataclass(frozen=True, slots=True)
class RuntimeAcceptanceFacts:
    """Trusted facts supplied by host runtime or verifier boundaries.

    Planner and model output must never construct this object. It represents
    observations that already crossed a trusted application/runtime boundary.
    """

    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    passed_tests: tuple[str, ...] = field(default_factory=tuple)
    human_approvals: tuple[str, ...] = field(default_factory=tuple)
    freshness_by_ref: dict[str, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "passed_tests", tuple(self.passed_tests))
        object.__setattr__(self, "human_approvals", tuple(self.human_approvals))
        object.__setattr__(self, "freshness_by_ref", dict(self.freshness_by_ref))
        for ref, verified_at in self.freshness_by_ref.items():
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("acceptance freshness ref must be non-empty")
            if not isinstance(verified_at, datetime):
                raise ValueError("acceptance freshness value must be a datetime")
            if verified_at.tzinfo is None or verified_at.utcoffset() is None:
                raise ValueError("acceptance freshness datetime must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    """Summary of one harness runtime execution."""

    run: TaskRun
    accepted: bool
    events: tuple[TaskEvent, ...]
    checkpoints: tuple[Checkpoint, ...]
    tool_results: tuple[ToolResult, ...]
    acceptance_decision: AcceptanceDecision | None = None
    failure: FailureEnvelope | None = None
    step_verifications: tuple[StepVerification, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RuntimeSubmission:
    """Immutable contract, plan, and created run persisted for later execution."""

    contract: TaskContract
    plan: ProgramPlan
    run: TaskRun


@dataclass(frozen=True, slots=True)
class _StepExecution:
    """Internal result for one step through the shared lifecycle path."""

    result: ToolResult
    failure: FailureEnvelope | None = None


class HarnessRuntime:
    """Single lifecycle path for deterministic harness execution."""

    def __init__(
        self,
        *,
        store: TaskStore,
        tools: ToolRegistry,
        acceptance_gate: AcceptanceGate | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        resume_policy: ResumePolicy | None = None,
        step_verifier: StepVerifier | None = None,
        long_task_store: LongTaskStore | None = None,
        outbox_fault_injector: OutboxFaultInjector | None = None,
    ) -> None:
        self._store = store
        self._tools = tools
        self._acceptance_gate = acceptance_gate or AcceptanceGate()
        self._recovery_policy = recovery_policy or RecoveryPolicy()
        self._resume_policy = resume_policy or ResumePolicy()
        self._step_verifier = step_verifier or StepVerifier()
        self._long_task_store = long_task_store
        self._outbox = SideEffectOutbox(
            long_task_store,
            fault_injector=outbox_fault_injector,
        )
        self._context_pack_builder = ContextPackBuilder(
            store,
            long_task_store=long_task_store,
        )

    async def run(
        self,
        *,
        contract: TaskContract,
        run_id: str,
        steps: tuple[RuntimeToolStep, ...] | list[RuntimeToolStep],
        acceptance_facts: RuntimeAcceptanceFacts | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        plan_metadata: dict[str, Any] | None = None,
        request_context: dict[str, Any] | None = None,
        interactive: bool = True,
    ) -> RuntimeResult:
        facts = acceptance_facts or RuntimeAcceptanceFacts()
        self.submit(
            contract=contract,
            run_id=run_id,
            steps=steps,
            model_name=model_name,
            prompt_version=prompt_version,
            plan_metadata=plan_metadata,
            request_context=request_context,
        )
        ordered_steps = tuple(steps)
        self._record_acceptance_facts(
            run_id,
            facts,
            idempotency_key=f"acceptance-facts:{run_id}:initial",
        )
        self._set_status(run_id, TaskRunStatus.RUNNING)

        tool_results: list[ToolResult] = []

        for step in ordered_steps:
            stopped = self._cooperative_stop_result(run_id, tool_results)
            if stopped is not None:
                return stopped
            execution = await self._execute_step(
                run_id=run_id,
                step=step,
                interactive=interactive,
                idempotency_anchor=f"tool:{step.id}",
            )
            tool_results.append(execution.result)
            if execution.failure is not None:
                return self._result(
                    run_id=run_id,
                    accepted=False,
                    tool_results=tool_results,
                    failure=execution.failure,
                )

        stopped = self._cooperative_stop_result(run_id, tool_results)
        if stopped is not None:
            return stopped

        return self._finish_acceptance(
            run_id=run_id,
            contract=contract,
            acceptance_facts=facts,
            tool_results=tool_results,
        )

    def submit(
        self,
        *,
        contract: TaskContract,
        run_id: str,
        steps: tuple[RuntimeToolStep, ...] | list[RuntimeToolStep],
        model_name: str | None = None,
        prompt_version: str | None = None,
        plan_metadata: dict[str, Any] | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> RuntimeSubmission:
        """Persist one immutable plan and leave its run claimable by a worker."""

        if not run_id.strip():
            raise ValueError("run_id must be non-empty")
        ordered_steps = tuple(steps)
        self._validate_linear_step_dependencies(ordered_steps)
        program_plan = ProgramPlan(
            id=f"plan_{run_id}",
            contract_id=contract.id,
            dag=PlanDAG(tuple(step.to_plan_step() for step in ordered_steps)),
            metadata={
                "model_name": model_name,
                "prompt_version": prompt_version,
                "planner": dict(plan_metadata or {}),
                "request_context": dict(request_context or {}),
            },
        )
        self._store.save_contract(contract)
        self._store.save_plan(program_plan)
        self._store.create_run(
            TaskRun(
                id=run_id,
                contract_id=contract.id,
                plan_id=program_plan.id,
                model_name=model_name,
                prompt_version=prompt_version,
            )
        )
        self._append_event(
            run_id=run_id,
            type=TaskEventType.RUN_CREATED,
            payload={
                "contract_id": contract.id,
                "plan_id": program_plan.id,
                "model_name": model_name,
                "prompt_version": prompt_version,
            },
        )
        persisted_run = self._store.get_run(run_id)
        if persisted_run is None:
            raise RuntimeError(f"submitted run disappeared: {run_id}")
        return RuntimeSubmission(
            contract=contract,
            plan=program_plan,
            run=persisted_run,
        )

    async def resume_incomplete_run(
        self,
        *,
        run_id: str,
        acceptance_facts: RuntimeAcceptanceFacts | None = None,
        interactive: bool = True,
    ) -> RuntimeResult:
        """Recover a created/running run from its persisted plan and events."""

        run, contract, program_plan = self._require_resume_state(run_id)
        if run.status not in {TaskRunStatus.CREATED, TaskRunStatus.RUNNING}:
            raise ValueError(
                "incomplete-run recovery requires created or running status"
            )
        updated = self._set_status(run_id, TaskRunStatus.RUNNING)
        if updated.status is not TaskRunStatus.RUNNING:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[],
            )
        facts = self._effective_acceptance_facts(
            run_id,
            acceptance_facts,
            idempotency_key=(
                f"acceptance-facts:{run_id}:incomplete:{updated.event_seq}"
            ),
        )
        context_pack = self._context_pack_builder.build(
            run_id=run_id,
            program_plan=program_plan,
        )
        statuses = dict(context_pack.metadata.get("step_statuses") or {})
        running_step_ids = tuple(
            step_id
            for step_id in program_plan.dag.topological_step_ids()
            if statuses.get(step_id) == StepStatus.RUNNING.value
        )
        results: list[ToolResult] = []
        if len(running_step_ids) > 1:
            return self._fail_stalled_continuation(
                run_id=run_id,
                program_plan=program_plan,
                context_pack=context_pack,
                tool_results=results,
            )
        if running_step_ids:
            step = RuntimeToolStep.from_plan_step(
                program_plan.step_by_id(running_step_ids[0])
            )
            execution = await self._execute_step(
                run_id=run_id,
                step=step,
                interactive=interactive,
                idempotency_anchor=(
                    f"incomplete:{context_pack.event_seq}:tool:{step.id}"
                ),
                event_context={
                    "worker_recovery": self._context_pack_anchor(context_pack),
                },
                checkpoint_context={
                    "recovered_incomplete_step_id": step.id,
                    "program_plan_id": program_plan.id,
                },
            )
            results.append(execution.result)
            if execution.failure is not None:
                return self._result(
                    run_id=run_id,
                    accepted=False,
                    tool_results=results,
                    failure=execution.failure,
                )
        return await self._continue_remaining_plan(
            run_id=run_id,
            contract=contract,
            program_plan=program_plan,
            acceptance_facts=facts,
            interactive=interactive,
            tool_results=results,
        )

    async def resume_from_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
        acceptance_facts: RuntimeAcceptanceFacts | None = None,
        interactive: bool = True,
    ) -> RuntimeResult:
        """Resume a failed run from its latest checkpoint when policy allows it."""

        _, contract, program_plan = self._require_resume_state(run_id)
        checkpoint = self._select_checkpoint(run_id, checkpoint_id)

        decision = self._resume_policy.decide(checkpoint)
        if not decision.can_resume:
            event_type = (
                TaskEventType.USER_INPUT_REQUIRED
                if decision.action in {
                    RecoveryAction.ASK_USER,
                    RecoveryAction.READ_BEFORE_WRITE,
                    RecoveryAction.USE_ALTERNATIVE_TOOL,
                    RecoveryAction.MANUAL_REVIEW,
                }
                else TaskEventType.RUN_FAILED
            )
            self._append_event(
                run_id=run_id,
                type=event_type,
                payload={
                    "checkpoint_id": checkpoint.id,
                    "can_resume": False,
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "failed_step": decision.failed_step,
                },
            )
            if event_type is TaskEventType.USER_INPUT_REQUIRED:
                self._set_status(
                    run_id,
                    TaskRunStatus.WAITING_USER,
                    checkpoint_id=checkpoint.id,
                )
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[],
                failure=checkpoint.failure,
            )

        if not decision.failed_step:
            raise ValueError("resume decision has no failed step")
        source_event = self._resume_source_event(run_id, checkpoint)
        step = RuntimeToolStep.from_plan_step(
            program_plan.step_by_id(decision.failed_step)
        )
        facts = self._effective_acceptance_facts(
            run_id,
            acceptance_facts,
            idempotency_key=f"acceptance-facts:{checkpoint.id}:retry",
        )
        updated = self._set_status(
            run_id,
            TaskRunStatus.RUNNING,
            checkpoint_id=checkpoint.id,
        )
        if updated.status is not TaskRunStatus.RUNNING:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[],
                failure=checkpoint.failure,
            )
        execution = await self._execute_step(
            run_id=run_id,
            step=step,
            interactive=interactive,
            idempotency_anchor=f"resume:{checkpoint.id}:tool:{step.id}",
            event_context={
                "resume": {
                    "checkpoint_id": checkpoint.id,
                    "source_event_id": source_event.id,
                    "action": decision.action.value,
                }
            },
            checkpoint_context={
                "retry_of_checkpoint_id": checkpoint.id,
            },
        )
        if execution.failure is not None:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[execution.result],
                failure=execution.failure,
            )
        return await self._continue_remaining_plan(
            run_id=run_id,
            contract=contract,
            program_plan=program_plan,
            acceptance_facts=facts,
            interactive=interactive,
            tool_results=[execution.result],
        )

    async def resume_with_alternative_tool(
        self,
        *,
        run_id: str,
        alternative_step: RuntimeToolStep,
        checkpoint_id: str | None = None,
        acceptance_facts: RuntimeAcceptanceFacts | None = None,
        interactive: bool = True,
    ) -> RuntimeResult:
        """Resume a checkpoint with an explicit alternative tool step."""

        _, contract, program_plan = self._require_resume_state(run_id)
        checkpoint = self._select_checkpoint(run_id, checkpoint_id)
        failure = checkpoint.failure
        if failure is None:
            raise ValueError("checkpoint has no failure envelope")
        if failure.recommended_action is not RecoveryAction.USE_ALTERNATIVE_TOOL:
            raise ValueError("checkpoint is not waiting for alternative tool")

        source_event = self._resume_source_event(run_id, checkpoint)
        original_step = RuntimeToolStep.from_plan_step(
            program_plan.step_by_id(failure.failed_step)
        )
        step = self._replacement_step(original_step, alternative_step)
        facts = self._effective_acceptance_facts(
            run_id,
            acceptance_facts,
            idempotency_key=f"acceptance-facts:{checkpoint.id}:alternative",
        )
        updated = self._set_status(
            run_id,
            TaskRunStatus.RUNNING,
            checkpoint_id=checkpoint.id,
        )
        if updated.status is not TaskRunStatus.RUNNING:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[],
                failure=checkpoint.failure,
            )
        execution = await self._execute_step(
            run_id=run_id,
            step=step,
            interactive=interactive,
            idempotency_anchor=f"alternative:{checkpoint.id}:tool:{step.id}",
            event_context={
                "alternative": {
                    "checkpoint_id": checkpoint.id,
                    "source_event_id": source_event.id,
                    "failed_step": failure.failed_step,
                    "replacement_for_tool": failure.metadata.get("tool_name"),
                    "provided_step_id": alternative_step.id,
                    "alternative_tool": alternative_step.tool_name,
                },
            },
            checkpoint_context={
                "alternative_of_checkpoint_id": checkpoint.id,
                "alternative_tool": alternative_step.tool_name,
            },
        )
        if execution.failure is not None:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[execution.result],
                failure=execution.failure,
            )
        return await self._continue_remaining_plan(
            run_id=run_id,
            contract=contract,
            program_plan=program_plan,
            acceptance_facts=facts,
            interactive=interactive,
            tool_results=[execution.result],
        )

    async def resume_with_user_approval(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
        feedback: str = "",
        acceptance_facts: RuntimeAcceptanceFacts | None = None,
        interactive: bool = True,
    ) -> RuntimeResult:
        """Resume an ask-user checkpoint after explicit user approval."""

        _, contract, program_plan = self._require_resume_state(run_id)
        checkpoint = self._select_checkpoint(run_id, checkpoint_id)
        failure = checkpoint.failure
        approval_ids = self._approval_criterion_ids(contract, checkpoint)
        if failure is None and not approval_ids:
            raise ValueError("checkpoint is not waiting for user approval")
        if failure is not None and failure.recommended_action is not RecoveryAction.ASK_USER:
            raise ValueError("checkpoint is not waiting for user approval")

        new_facts = self._merge_acceptance_facts(
            acceptance_facts or RuntimeAcceptanceFacts(),
            RuntimeAcceptanceFacts(human_approvals=approval_ids),
        )
        facts = self._effective_acceptance_facts(
            run_id,
            new_facts,
            idempotency_key=f"acceptance-facts:{checkpoint.id}:approval",
        )
        self._resolve_checkpoint_interaction(
            checkpoint,
            answer=feedback.strip() or "approved",
        )
        self._append_event(
            run_id=run_id,
            type=TaskEventType.USER_INPUT_RECORDED,
            payload={
                "checkpoint_id": checkpoint.id,
                "approved": True,
                "feedback": feedback,
                "failed_step": failure.failed_step if failure else None,
                "approved_criteria": list(approval_ids),
            },
        )
        updated = self._set_status(
            run_id,
            TaskRunStatus.RUNNING,
            checkpoint_id=checkpoint.id,
        )
        if updated.status is not TaskRunStatus.RUNNING:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[],
                failure=checkpoint.failure,
            )

        if failure is None:
            return await self._continue_remaining_plan(
                run_id=run_id,
                contract=contract,
                program_plan=program_plan,
                acceptance_facts=facts,
                interactive=interactive,
                tool_results=[],
            )

        source_event = self._resume_source_event(run_id, checkpoint)
        original_step = RuntimeToolStep.from_plan_step(
            program_plan.step_by_id(failure.failed_step)
        )
        step = self._approved_step(original_step)
        execution = await self._execute_step(
            run_id=run_id,
            step=step,
            interactive=interactive,
            idempotency_anchor=f"approval:{checkpoint.id}:tool:{step.id}",
            event_context={
                "user_approval": {
                    "checkpoint_id": checkpoint.id,
                    "source_event_id": source_event.id,
                },
            },
            checkpoint_context={
                "approval_of_checkpoint_id": checkpoint.id,
            },
        )
        if execution.failure is not None:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[execution.result],
                failure=execution.failure,
            )
        return await self._continue_remaining_plan(
            run_id=run_id,
            contract=contract,
            program_plan=program_plan,
            acceptance_facts=facts,
            interactive=interactive,
            tool_results=[execution.result],
        )

    async def _continue_remaining_plan(
        self,
        *,
        run_id: str,
        contract: TaskContract,
        program_plan: ProgramPlan,
        acceptance_facts: RuntimeAcceptanceFacts,
        interactive: bool,
        tool_results: list[ToolResult],
    ) -> RuntimeResult:
        """Continue the persisted unfinished frontier through one linear path."""

        results = list(tool_results)
        while True:
            stopped = self._cooperative_stop_result(run_id, results)
            if stopped is not None:
                return stopped
            context_pack = self._context_pack_builder.build(
                run_id=run_id,
                program_plan=program_plan,
            )
            incomplete = self._incomplete_step_ids(program_plan, context_pack)
            if not incomplete:
                return self._finish_acceptance(
                    run_id=run_id,
                    contract=contract,
                    acceptance_facts=acceptance_facts,
                    tool_results=results,
                )
            if context_pack.pending_interactions:
                self._set_status(run_id, TaskRunStatus.WAITING_USER)
                checkpoint = self._store.latest_checkpoint(run_id)
                return self._result(
                    run_id=run_id,
                    accepted=False,
                    tool_results=results,
                    failure=checkpoint.failure if checkpoint else None,
                )
            if not context_pack.frontier_step_ids:
                return self._fail_stalled_continuation(
                    run_id=run_id,
                    program_plan=program_plan,
                    context_pack=context_pack,
                    tool_results=results,
                )

            step_id = context_pack.frontier_step_ids[0]
            step = RuntimeToolStep.from_plan_step(program_plan.step_by_id(step_id))
            execution = await self._execute_step(
                run_id=run_id,
                step=step,
                interactive=interactive,
                idempotency_anchor=f"continuation:{step.id}",
                event_context={
                    "continuation": self._context_pack_anchor(context_pack),
                },
                checkpoint_context={
                    "program_plan_id": program_plan.id,
                    "continued_from_event_seq": context_pack.event_seq,
                },
            )
            results.append(execution.result)
            if execution.failure is not None:
                return self._result(
                    run_id=run_id,
                    accepted=False,
                    tool_results=results,
                    failure=execution.failure,
                )

    async def _execute_step(
        self,
        *,
        run_id: str,
        step: RuntimeToolStep,
        interactive: bool,
        idempotency_anchor: str,
        event_context: dict[str, Any] | None = None,
        checkpoint_context: dict[str, Any] | None = None,
    ) -> _StepExecution:
        """Execute one step through the shared event/checkpoint lifecycle."""

        plan_step = step.to_plan_step()
        self._append_step_started(run_id, plan_step, payload=event_context)
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        if run.plan_id is None:
            raise ValueError(f"run has no persisted plan: {run_id}")
        logical_side_effect_key = (
            f"run:{run_id}:plan:{run.plan_id}:step:{step.id}"
        )
        prepared_call = self._tools.prepare(
            step.tool_name,
            step.arguments,
            interactive=interactive,
            allow_confirm=step.allow_confirm,
            idempotency_key=logical_side_effect_key,
        )
        outbox = self._outbox.prepare(
            run_id=run_id,
            plan_id=run.plan_id,
            step_id=step.id,
            call=prepared_call,
        )
        if outbox.action is OutboxAction.DISPATCH:
            self._outbox.after_intent_persisted(outbox)
            outbox = self._outbox.begin_dispatch(outbox)
            result = await self._tools.execute_prepared(prepared_call)
            self._outbox.after_dispatch(outbox)
            outbox, result = self._outbox.record_result(
                outbox,
                result,
                expected_intents=prepared_call.side_effect_intents,
            )
            self._outbox.before_result_commit(outbox)
        elif outbox.action in {OutboxAction.REPLAY_RESULT, OutboxAction.BLOCK}:
            if outbox.result is None:
                raise RuntimeError("outbox decision has no result")
            result = outbox.result
        else:
            result = await self._tools.execute_prepared(prepared_call)
            if result.side_effects:
                outbox, result = self._outbox.record_unplanned_result(
                    run_id=run_id,
                    plan_id=run.plan_id,
                    step_id=step.id,
                    call=prepared_call,
                    result=result,
                )
        payload = {
            "step_id": step.id,
            "tool_name": step.tool_name,
            "tool_arguments": dict(step.arguments),
            "step": self._plan_step_payload(plan_step),
            "result": result.to_metadata(),
            "content": result.content,
            "error": result.error,
            "side_effect_outbox": {
                "action": outbox.action.value,
                "record_ids": [record.id for record in outbox.records],
                "statuses": [record.status.value for record in outbox.records],
            },
        }
        if event_context:
            payload.update(event_context)
        tool_event = self._append_event(
            run_id=run_id,
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload=payload,
            evidence=result.evidence,
            side_effects=result.side_effects,
            idempotency_key=idempotency_anchor,
        )
        if outbox.action is not OutboxAction.BYPASS:
            outbox = self._outbox.confirm_result(
                outbox,
                result_event_id=tool_event.id,
            )

        checkpoint_state = {
            "step_id": step.id,
            "tool_name": step.tool_name,
        }
        if checkpoint_context:
            checkpoint_state.update(checkpoint_context)

        if not result.ok:
            failure = self._recovery_policy.recommend_from_tool_result(
                tool_name=step.tool_name,
                result=result,
                failed_step=step.id,
            )
            current_run = self._store.get_run(run_id)
            if current_run is None:
                raise ValueError(f"unknown run id: {run_id}")
            checkpoint_status = (
                self._checkpoint_status_for_run(current_run.status)
                if current_run.status
                in {TaskRunStatus.PAUSED, TaskRunStatus.CANCELLED}
                else self._checkpoint_status_for_run(failure.status)
            )
            checkpoint = self._create_checkpoint(
                run_id=run_id,
                status=checkpoint_status,
                state={"failed_step_id": step.id, **checkpoint_state},
                resume_from_event_id=tool_event.id,
                failure=failure,
            )
            self._append_step_verified(
                run_id,
                self._step_verification_from_failure(
                    plan_step=plan_step,
                    failure=failure,
                    result=result,
                    checkpoint_id=checkpoint.id,
                ),
                plan_step=plan_step,
            )
            updated = self._set_status(
                run_id,
                failure.status,
                checkpoint_id=checkpoint.id,
            )
            if updated.status is failure.status:
                self._append_terminal_failure_event(run_id, failure)
                self._open_interaction_for_failure(
                    run_id=run_id,
                    step_id=step.id,
                    checkpoint=checkpoint,
                    failure=failure,
                )
            return _StepExecution(result=result, failure=failure)

        step_failure = self._verify_successful_step(
            run_id=run_id,
            step=step,
            result=result,
            tool_event=tool_event,
            checkpoint_state={
                "completed_step_id": step.id,
                **checkpoint_state,
            },
        )
        return _StepExecution(result=result, failure=step_failure)

    def _open_interaction_for_failure(
        self,
        *,
        run_id: str,
        step_id: str,
        checkpoint: Checkpoint,
        failure: FailureEnvelope,
    ) -> PendingInteraction | None:
        if failure.recommended_action is not RecoveryAction.ASK_USER:
            return None
        kind = (
            InteractionKind.USER_APPROVAL
            if failure.metadata.get("tool_status") == "requires_confirmation"
            else InteractionKind.USER_INPUT
        )
        return self._open_interaction(
            run_id=run_id,
            checkpoint=checkpoint,
            question=failure.root_cause,
            required_by_step_id=step_id,
            kind=kind,
            metadata={
                "failure_type": failure.failure_type.value,
                "recommended_action": failure.recommended_action.value,
            },
        )

    def _open_interaction(
        self,
        *,
        run_id: str,
        checkpoint: Checkpoint,
        question: str,
        required_by_step_id: str | None,
        kind: InteractionKind,
        metadata: dict[str, Any] | None = None,
    ) -> PendingInteraction | None:
        if self._long_task_store is None:
            return None
        interaction_id = f"interaction_{checkpoint.id}"
        existing = self._long_task_store.get_pending_interaction(interaction_id)
        if existing is not None:
            return existing
        return self._long_task_store.save_pending_interaction(
            PendingInteraction(
                id=interaction_id,
                run_id=run_id,
                checkpoint_id=checkpoint.id,
                question=question or "User input is required to continue.",
                required_by_step_id=required_by_step_id,
                kind=kind,
                metadata=metadata or {},
            )
        )

    def _require_resume_state(
        self,
        run_id: str,
    ) -> tuple[TaskRun, TaskContract, ProgramPlan]:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        contract = self._store.get_contract(run.contract_id)
        if contract is None:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        return run, contract, self._require_program_plan(run)

    def _require_program_plan(self, run: TaskRun) -> ProgramPlan:
        if run.plan_id is None:
            raise ValueError(f"run has no persisted plan: {run.id}")
        program_plan = self._store.get_plan(run.plan_id)
        if program_plan is None:
            raise ValueError(f"unknown plan id: {run.plan_id}")
        if program_plan.contract_id != run.contract_id:
            raise ValueError("persisted plan does not match run contract")
        return program_plan

    def _select_checkpoint(
        self,
        run_id: str,
        checkpoint_id: str | None,
    ) -> Checkpoint:
        checkpoint = (
            self._store.get_checkpoint(checkpoint_id)
            if checkpoint_id is not None
            else self._store.latest_checkpoint(run_id)
        )
        if checkpoint is None:
            raise ValueError(f"run has no checkpoint: {run_id}")
        if checkpoint.run_id != run_id:
            raise ValueError("checkpoint does not belong to run")
        return checkpoint

    @staticmethod
    def _replacement_step(
        original: RuntimeToolStep,
        alternative: RuntimeToolStep,
    ) -> RuntimeToolStep:
        """Replace execution capability without mutating the persisted plan."""

        return RuntimeToolStep(
            id=original.id,
            tool_name=alternative.tool_name,
            arguments=alternative.arguments,
            allow_confirm=alternative.allow_confirm,
            title=original.title,
            expected_output=original.expected_output,
            verification=original.verification,
            depends_on=original.depends_on,
            required_evidence_refs=original.required_evidence_refs,
        )

    @staticmethod
    def _approved_step(original: RuntimeToolStep) -> RuntimeToolStep:
        return RuntimeToolStep(
            id=original.id,
            tool_name=original.tool_name,
            arguments=original.arguments,
            allow_confirm=True,
            title=original.title,
            expected_output=original.expected_output,
            verification=original.verification,
            depends_on=original.depends_on,
            required_evidence_refs=original.required_evidence_refs,
        )

    @staticmethod
    def _approval_criterion_ids(
        contract: TaskContract,
        checkpoint: Checkpoint,
    ) -> tuple[str, ...]:
        blocked = {
            str(item)
            for item in checkpoint.state.get("blocked_criteria") or ()
        }
        return tuple(
            criterion.id
            for criterion in contract.acceptance_criteria
            if criterion.id in blocked
            and criterion.type is CriterionType.HUMAN_APPROVAL
        )

    def _resolve_checkpoint_interaction(
        self,
        checkpoint: Checkpoint,
        *,
        answer: str,
    ) -> None:
        if self._long_task_store is None:
            return
        for interaction in self._long_task_store.list_pending_interactions(
            checkpoint.run_id
        ):
            if interaction.checkpoint_id == checkpoint.id and interaction.open:
                self._long_task_store.resolve_pending_interaction(
                    interaction.id,
                    answer,
                )

    def _effective_acceptance_facts(
        self,
        run_id: str,
        supplied: RuntimeAcceptanceFacts | None,
        *,
        idempotency_key: str,
    ) -> RuntimeAcceptanceFacts:
        persisted = self._acceptance_facts_from_events(run_id)
        new_facts = supplied or RuntimeAcceptanceFacts()
        self._record_acceptance_facts(
            run_id,
            new_facts,
            idempotency_key=idempotency_key,
        )
        return self._merge_acceptance_facts(persisted, new_facts)

    def _record_acceptance_facts(
        self,
        run_id: str,
        facts: RuntimeAcceptanceFacts,
        *,
        idempotency_key: str,
    ) -> None:
        if not (
            facts.evidence_refs
            or facts.passed_tests
            or facts.human_approvals
            or facts.freshness_by_ref
        ):
            return
        self._append_event(
            run_id=run_id,
            type=TaskEventType.ACCEPTANCE_FACTS_RECORDED,
            payload={
                "evidence_refs": list(facts.evidence_refs),
                "passed_tests": list(facts.passed_tests),
                "human_approvals": list(facts.human_approvals),
                "freshness_by_ref": {
                    ref: verified_at.isoformat()
                    for ref, verified_at in facts.freshness_by_ref.items()
                },
            },
            idempotency_key=idempotency_key,
        )

    def _acceptance_facts_from_events(self, run_id: str) -> RuntimeAcceptanceFacts:
        facts = RuntimeAcceptanceFacts()
        for event in self._store.list_events(run_id):
            if event.type is not TaskEventType.ACCEPTANCE_FACTS_RECORDED:
                continue
            freshness = {
                str(ref): datetime.fromisoformat(str(value))
                for ref, value in dict(
                    event.payload.get("freshness_by_ref") or {}
                ).items()
            }
            facts = self._merge_acceptance_facts(
                facts,
                RuntimeAcceptanceFacts(
                    evidence_refs=tuple(
                        str(item)
                        for item in event.payload.get("evidence_refs") or ()
                    ),
                    passed_tests=tuple(
                        str(item)
                        for item in event.payload.get("passed_tests") or ()
                    ),
                    human_approvals=tuple(
                        str(item)
                        for item in event.payload.get("human_approvals") or ()
                    ),
                    freshness_by_ref=freshness,
                ),
            )
        return facts

    @staticmethod
    def _merge_acceptance_facts(
        *items: RuntimeAcceptanceFacts,
    ) -> RuntimeAcceptanceFacts:
        evidence_refs: dict[str, None] = {}
        passed_tests: dict[str, None] = {}
        human_approvals: dict[str, None] = {}
        freshness_by_ref: dict[str, datetime] = {}
        for item in items:
            evidence_refs.update(dict.fromkeys(item.evidence_refs))
            passed_tests.update(dict.fromkeys(item.passed_tests))
            human_approvals.update(dict.fromkeys(item.human_approvals))
            freshness_by_ref.update(item.freshness_by_ref)
        return RuntimeAcceptanceFacts(
            evidence_refs=tuple(evidence_refs),
            passed_tests=tuple(passed_tests),
            human_approvals=tuple(human_approvals),
            freshness_by_ref=freshness_by_ref,
        )

    @staticmethod
    def _incomplete_step_ids(
        program_plan: ProgramPlan,
        context_pack: ContextPack,
    ) -> tuple[str, ...]:
        statuses = dict(context_pack.metadata.get("step_statuses") or {})
        return tuple(
            step_id
            for step_id in program_plan.dag.topological_step_ids()
            if statuses.get(step_id) != StepStatus.PASSED.value
        )

    @staticmethod
    def _context_pack_anchor(context_pack: ContextPack) -> dict[str, Any]:
        return {
            "program_plan_id": context_pack.program_plan_id,
            "event_seq": context_pack.event_seq,
            "checkpoint_id": context_pack.checkpoint_id,
            "frontier_step_ids": list(context_pack.frontier_step_ids),
            "completed_step_ids": list(context_pack.completed_step_ids),
            "pending_interaction_ids": [
                str(item.get("id"))
                for item in context_pack.pending_interactions
            ],
        }

    def _fail_stalled_continuation(
        self,
        *,
        run_id: str,
        program_plan: ProgramPlan,
        context_pack: ContextPack,
        tool_results: list[ToolResult],
    ) -> RuntimeResult:
        incomplete = self._incomplete_step_ids(program_plan, context_pack)
        failure = FailureEnvelope(
            status=TaskRunStatus.FAILED,
            failed_step=incomplete[0] if incomplete else "plan",
            failure_type=FailureType.UNKNOWN,
            root_cause="persisted plan has unfinished steps but no executable frontier",
            recoverable=False,
            recommended_action=RecoveryAction.MANUAL_REVIEW,
            visibility=FailureVisibility.IMPLICIT,
            duration=FailureDuration.PERMANENT,
            metadata={
                "program_plan_id": program_plan.id,
                "incomplete_step_ids": list(incomplete),
                "failed_step_ids": list(context_pack.failed_step_ids),
                "blocked_step_ids": list(context_pack.blocked_step_ids),
            },
        )
        events = self._store.list_events(run_id)
        if not events:
            raise RuntimeError("run has no event anchor for stalled continuation")
        checkpoint = self._create_checkpoint(
            run_id=run_id,
            status=CheckpointStatus.FAILED,
            state={
                "program_plan_id": program_plan.id,
                "incomplete_step_ids": list(incomplete),
            },
            resume_from_event_id=events[-1].id,
            failure=failure,
        )
        updated = self._set_status(
            run_id,
            TaskRunStatus.FAILED,
            checkpoint_id=checkpoint.id,
        )
        if updated.status is TaskRunStatus.FAILED:
            self._append_terminal_failure_event(run_id, failure)
        return self._result(
            run_id=run_id,
            accepted=False,
            tool_results=tool_results,
            failure=failure,
        )

    def _append_event(
        self,
        *,
        run_id: str,
        type: TaskEventType,
        payload: dict[str, Any] | None = None,
        evidence: tuple[Any, ...] | list[Any] = (),
        side_effects: tuple[Any, ...] | list[Any] = (),
        idempotency_key: str | None = None,
    ) -> TaskEvent:
        return self._store.append_event(
            run_id=run_id,
            type=type,
            payload=payload,
            evidence=evidence,
            side_effects=side_effects,
            idempotency_key=idempotency_key,
        )

    def _append_step_started(
        self,
        run_id: str,
        step: PlanStep,
        *,
        payload: dict[str, Any] | None = None,
    ) -> TaskEvent:
        data = {
            "step_id": step.id,
            "status": StepStatus.RUNNING.value,
            "step": self._plan_step_payload(step),
        }
        if payload:
            data.update(payload)
        return self._append_event(
            run_id=run_id,
            type=TaskEventType.PLAN_STEP_STARTED,
            payload=data,
        )

    def _append_step_verified(
        self,
        run_id: str,
        verification: StepVerification,
        *,
        plan_step: PlanStep,
    ) -> TaskEvent:
        payload = verification.to_dict()
        payload["step"] = self._plan_step_payload(plan_step)
        return self._append_event(
            run_id=run_id,
            type=TaskEventType.PLAN_STEP_VERIFIED,
            payload=payload,
        )

    def _verify_successful_step(
        self,
        *,
        run_id: str,
        step: RuntimeToolStep,
        result: ToolResult,
        tool_event: TaskEvent,
        checkpoint_state: dict[str, Any],
    ) -> FailureEnvelope | None:
        plan_step = step.to_plan_step()
        evidence_refs = tuple(item.ref for item in result.evidence)
        current_run = self._store.get_run(run_id)
        if current_run is None:
            raise ValueError(f"unknown run id: {run_id}")
        controlled_status = current_run.status in {
            TaskRunStatus.PAUSED,
            TaskRunStatus.CANCELLED,
        }
        verification = self._step_verifier.verify(
            plan_step,
            tool_ok=True,
            evidence_refs=evidence_refs,
        )
        if not verification.passed:
            failure = FailureEnvelope(
                status=TaskRunStatus.FAILED,
                failed_step=step.id,
                failure_type=FailureType.STEP_VERIFICATION_FAILED,
                root_cause=verification.reason,
                recoverable=False,
                recommended_action=RecoveryAction.STOP,
                evidence=result.evidence,
                side_effects=result.side_effects,
                visibility=FailureVisibility.IMPLICIT,
                duration=FailureDuration.PERMANENT,
                metadata={
                    "required_evidence_refs": list(plan_step.required_evidence_refs),
                    "missing_evidence_refs": list(verification.missing_evidence_refs),
                    "observed_evidence_refs": list(verification.evidence_refs),
                },
            )
            checkpoint = self._create_checkpoint(
                run_id=run_id,
                status=(
                    self._checkpoint_status_for_run(current_run.status)
                    if controlled_status
                    else CheckpointStatus.FAILED
                ),
                state={
                    **dict(checkpoint_state),
                    "failed_step_id": step.id,
                    "missing_evidence_refs": list(verification.missing_evidence_refs),
                },
                resume_from_event_id=tool_event.id,
                failure=failure,
            )
            self._append_step_verified(
                run_id,
                self._step_verifier.verify(
                    plan_step,
                    tool_ok=True,
                    evidence_refs=evidence_refs,
                    checkpoint_id=checkpoint.id,
                ),
                plan_step=plan_step,
            )
            updated = self._set_status(
                run_id,
                TaskRunStatus.FAILED,
                checkpoint_id=checkpoint.id,
            )
            if updated.status is TaskRunStatus.FAILED:
                self._append_terminal_failure_event(run_id, failure)
            return failure

        checkpoint = self._create_checkpoint(
            run_id=run_id,
            status=(
                self._checkpoint_status_for_run(current_run.status)
                if controlled_status
                else CheckpointStatus.RUNNING
            ),
            state=checkpoint_state,
            resume_from_event_id=tool_event.id,
        )
        self._append_step_verified(
            run_id,
            self._step_verifier.verify(
                plan_step,
                tool_ok=True,
                evidence_refs=evidence_refs,
                checkpoint_id=checkpoint.id,
            ),
            plan_step=plan_step,
        )
        return None

    def _step_verification_from_failure(
        self,
        *,
        plan_step: PlanStep,
        failure: FailureEnvelope,
        result: ToolResult,
        checkpoint_id: str,
    ) -> StepVerification:
        status = (
            StepStatus.BLOCKED
            if failure.status is TaskRunStatus.WAITING_USER
            else StepStatus.FAILED
        )
        return StepVerification(
            step_id=plan_step.id,
            status=status,
            reason=failure.root_cause,
            evidence_refs=tuple(item.ref for item in result.evidence),
            checkpoint_id=checkpoint_id,
            metadata={
                "failure_type": failure.failure_type.value,
                "recommended_action": failure.recommended_action.value,
            },
        )

    @staticmethod
    def _plan_step_payload(step: PlanStep) -> dict[str, Any]:
        return {
            "id": step.id,
            "title": step.title,
            "expected_output": step.expected_output,
            "verification": step.verification,
            "depends_on": list(step.depends_on),
            "required_evidence_refs": list(step.required_evidence_refs),
            "metadata": dict(step.metadata),
        }

    @staticmethod
    def _validate_linear_step_dependencies(steps: tuple[RuntimeToolStep, ...]) -> None:
        dag = PlanDAG(tuple(step.to_plan_step() for step in steps))
        dag.validate_linear_order(tuple(step.id for step in steps))

    def _create_checkpoint(
        self,
        *,
        run_id: str,
        status: CheckpointStatus,
        state: dict[str, Any],
        resume_from_event_id: str,
        failure: FailureEnvelope | None = None,
    ) -> Checkpoint:
        checkpoint = self._store.create_checkpoint(
            run_id=run_id,
            status=status,
            state=state,
            resume_from_event_id=resume_from_event_id,
            failure=failure,
        )
        self._append_event(
            run_id=run_id,
            type=TaskEventType.CHECKPOINT_CREATED,
            payload={
                "checkpoint_id": checkpoint.id,
                "status": checkpoint.status.value,
            },
        )
        return checkpoint

    def _set_status(
        self,
        run_id: str,
        status: TaskRunStatus,
        *,
        checkpoint_id: str | None = None,
    ) -> TaskRun:
        current = self._store.get_run(run_id)
        if current is None:
            raise ValueError(f"unknown run id: {run_id}")
        if (
            current.status in {TaskRunStatus.PAUSED, TaskRunStatus.CANCELLED}
            and current.status is not status
        ):
            return current
        updated = self._store.compare_and_set_run_status(
            run_id,
            expected_statuses=(current.status,),
            status=status,
            checkpoint_id=checkpoint_id,
        )
        if updated is None:
            latest = self._store.get_run(run_id)
            if latest is None:
                raise ValueError(f"unknown run id: {run_id}")
            if latest.status in {TaskRunStatus.PAUSED, TaskRunStatus.CANCELLED}:
                return latest
            raise RuntimeError(
                f"run status changed concurrently: {current.status.value} -> "
                f"{latest.status.value}"
            )
        self._append_event(
            run_id=run_id,
            type=TaskEventType.STATUS_CHANGED,
            payload={
                "status": updated.status.value,
                "checkpoint_id": updated.current_checkpoint_id,
            },
        )
        latest = self._store.get_run(run_id)
        if latest is None:
            raise ValueError(f"unknown run id: {run_id}")
        return latest

    def _cooperative_stop_result(
        self,
        run_id: str,
        tool_results: list[ToolResult],
    ) -> RuntimeResult | None:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        if run.status not in {TaskRunStatus.PAUSED, TaskRunStatus.CANCELLED}:
            return None
        return self._result(
            run_id=run_id,
            accepted=False,
            tool_results=tool_results,
        )

    def _append_terminal_failure_event(
        self,
        run_id: str,
        failure: FailureEnvelope,
    ) -> None:
        event_type = (
            TaskEventType.USER_INPUT_REQUIRED
            if failure.status is TaskRunStatus.WAITING_USER
            else TaskEventType.RUN_FAILED
        )
        self._append_event(
            run_id=run_id,
            type=event_type,
            payload=failure.to_dict(),
        )

    def _resume_source_event(
        self,
        run_id: str,
        checkpoint: Checkpoint,
    ) -> TaskEvent:
        if checkpoint.resume_from_event_id is None:
            raise ValueError("checkpoint has no resume_from_event_id")
        for event in self._store.list_events(run_id):
            if event.id == checkpoint.resume_from_event_id:
                if event.type is not TaskEventType.TOOL_RESULT_RECORDED:
                    raise ValueError("resume source event is not a tool result")
                if not event.payload.get("tool_name"):
                    raise ValueError("resume source event has no tool name")
                return event
        raise ValueError(f"resume source event not found: {checkpoint.resume_from_event_id}")

    def _finish_acceptance(
        self,
        *,
        run_id: str,
        contract: TaskContract,
        acceptance_facts: RuntimeAcceptanceFacts,
        tool_results: list[ToolResult],
    ) -> RuntimeResult:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        program_plan = self._require_program_plan(run)
        context_pack = self._context_pack_builder.build(
            run_id=run_id,
            program_plan=program_plan,
        )
        incomplete = self._incomplete_step_ids(program_plan, context_pack)
        if incomplete:
            raise RuntimeError(
                "cannot evaluate acceptance before all plan steps pass: "
                + ", ".join(incomplete)
            )
        acceptance_facts = self._merge_acceptance_facts(
            self._acceptance_facts_from_events(run_id),
            acceptance_facts,
        )
        evidence_refs = set(acceptance_facts.evidence_refs)
        for event in self._store.list_events(run_id):
            if (
                event.type is TaskEventType.TOOL_RESULT_RECORDED
                and event.payload.get("result", {}).get("ok") is False
            ):
                continue
            evidence_refs.update(item.ref for item in event.evidence)
        for result in tool_results:
            evidence_refs.update(item.ref for item in result.evidence)
        decision = self._acceptance_gate.evaluate(
            contract,
            evidence_refs=evidence_refs,
            passed_tests=set(acceptance_facts.passed_tests),
            human_approvals=set(acceptance_facts.human_approvals),
            freshness_by_ref=acceptance_facts.freshness_by_ref,
        )
        acceptance_event = self._append_event(
            run_id=run_id,
            type=TaskEventType.ACCEPTANCE_EVALUATED,
            payload={
                "accepted": decision.accepted,
                "status": decision.status.value,
                "reason": decision.reason,
                "passed_criteria": list(decision.passed_criteria),
                "failed_criteria": list(decision.failed_criteria),
                "blocked_criteria": list(decision.blocked_criteria),
                "optional_failed_criteria": list(decision.optional_failed_criteria),
            },
        )
        if decision.accepted:
            checkpoint = self._create_checkpoint(
                run_id=run_id,
                status=CheckpointStatus.COMPLETED,
                state={"accepted": True},
                resume_from_event_id=acceptance_event.id,
            )
            updated = self._set_status(
                run_id,
                TaskRunStatus.COMPLETED,
                checkpoint_id=checkpoint.id,
            )
            if updated.status is not TaskRunStatus.COMPLETED:
                return self._result(
                    run_id=run_id,
                    accepted=False,
                    tool_results=tool_results,
                    acceptance_decision=decision,
                )
            self._append_event(
                run_id=run_id,
                type=TaskEventType.RUN_COMPLETED,
                payload={"reason": decision.reason},
            )
            return self._result(
                run_id=run_id,
                accepted=True,
                tool_results=tool_results,
                acceptance_decision=decision,
            )
        if decision.status is AcceptanceStatus.BLOCKED:
            criterion_types = {
                criterion.id: criterion.type
                for criterion in contract.acceptance_criteria
            }
            approval_only = all(
                criterion_types.get(criterion_id) is CriterionType.HUMAN_APPROVAL
                for criterion_id in decision.blocked_criteria
            )
            interaction_kind = (
                InteractionKind.USER_APPROVAL
                if approval_only
                else InteractionKind.USER_INPUT
            )
            checkpoint = self._create_checkpoint(
                run_id=run_id,
                status=CheckpointStatus.WAITING_USER,
                state={
                    "accepted": False,
                    "blocked_criteria": list(decision.blocked_criteria),
                },
                resume_from_event_id=acceptance_event.id,
            )
            self._open_interaction(
                run_id=run_id,
                checkpoint=checkpoint,
                question="Acceptance requires user approval or additional input.",
                required_by_step_id=None,
                kind=interaction_kind,
                metadata={
                    "blocked_criteria": list(decision.blocked_criteria),
                    "acceptance_wait": True,
                },
            )
            updated = self._set_status(
                run_id,
                TaskRunStatus.WAITING_USER,
                checkpoint_id=checkpoint.id,
            )
            if updated.status is not TaskRunStatus.WAITING_USER:
                return self._result(
                    run_id=run_id,
                    accepted=False,
                    tool_results=tool_results,
                    acceptance_decision=decision,
                )
            self._append_event(
                run_id=run_id,
                type=TaskEventType.USER_INPUT_REQUIRED,
                payload={"blocked_criteria": list(decision.blocked_criteria)},
            )
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=tool_results,
                acceptance_decision=decision,
            )
        failure = FailureEnvelope(
            status=TaskRunStatus.ACCEPTANCE_FAILED,
            failed_step="acceptance",
            failure_type=FailureType.ACCEPTANCE_FAILED,
            root_cause=decision.reason,
            recoverable=False,
            recommended_action=RecoveryAction.STOP,
            visibility=FailureVisibility.IMPLICIT,
            duration=FailureDuration.PERMANENT,
            metadata={
                "failed_criteria": list(decision.failed_criteria),
                "blocked_criteria": list(decision.blocked_criteria),
            },
        )
        checkpoint = self._create_checkpoint(
            run_id=run_id,
            status=CheckpointStatus.ACCEPTANCE_FAILED,
            state={
                "accepted": False,
                "failed_criteria": list(decision.failed_criteria),
            },
            resume_from_event_id=acceptance_event.id,
            failure=failure,
        )
        updated = self._set_status(
            run_id,
            TaskRunStatus.ACCEPTANCE_FAILED,
            checkpoint_id=checkpoint.id,
        )
        if updated.status is not TaskRunStatus.ACCEPTANCE_FAILED:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=tool_results,
                acceptance_decision=decision,
                failure=failure,
            )
        self._append_event(
            run_id=run_id,
            type=TaskEventType.RUN_FAILED,
            payload=failure.to_dict(),
        )
        return self._result(
            run_id=run_id,
            accepted=False,
            tool_results=tool_results,
            acceptance_decision=decision,
            failure=failure,
        )

    @staticmethod
    def _checkpoint_status_for_run(status: TaskRunStatus) -> CheckpointStatus:
        if status is TaskRunStatus.WAITING_USER:
            return CheckpointStatus.WAITING_USER
        if status is TaskRunStatus.PAUSED:
            return CheckpointStatus.PAUSED
        if status is TaskRunStatus.ACCEPTANCE_FAILED:
            return CheckpointStatus.ACCEPTANCE_FAILED
        if status is TaskRunStatus.COMPLETED:
            return CheckpointStatus.COMPLETED
        if status is TaskRunStatus.CANCELLED:
            return CheckpointStatus.CANCELLED
        return CheckpointStatus.FAILED

    def _result(
        self,
        *,
        run_id: str,
        accepted: bool,
        tool_results: list[ToolResult],
        acceptance_decision: AcceptanceDecision | None = None,
        failure: FailureEnvelope | None = None,
    ) -> RuntimeResult:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        return RuntimeResult(
            run=run,
            accepted=accepted,
            events=self._store.list_events(run_id),
            checkpoints=self._store.list_checkpoints(run_id),
            tool_results=tuple(tool_results),
            acceptance_decision=acceptance_decision,
            failure=failure,
            step_verifications=self._step_verifications(run_id),
        )

    def _step_verifications(self, run_id: str) -> tuple[StepVerification, ...]:
        verifications: list[StepVerification] = []
        for event in self._store.list_events(run_id):
            if event.type is not TaskEventType.PLAN_STEP_VERIFIED:
                continue
            payload = event.payload
            status_value = str(payload.get("status") or StepStatus.PENDING.value)
            verifications.append(
                StepVerification(
                    step_id=str(payload.get("step_id") or ""),
                    status=StepStatus(status_value),
                    reason=str(payload.get("reason") or ""),
                    evidence_refs=tuple(
                        str(item) for item in payload.get("evidence_refs") or ()
                    ),
                    missing_evidence_refs=tuple(
                        str(item)
                        for item in payload.get("missing_evidence_refs") or ()
                    ),
                    checkpoint_id=payload.get("checkpoint_id"),
                    metadata=dict(payload.get("metadata") or {}),
                )
            )
        return tuple(verifications)
