"""Minimal harness run lifecycle.

This module intentionally does not call an LLM. It executes deterministic
runtime steps so the project can verify run state, tool boundaries, events,
checkpoints, and acceptance before adding model planning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from re_zlagent.harness.storage import TaskStore
from re_zlagent.harness.tasking import (
    AcceptanceDecision,
    AcceptanceGate,
    AcceptanceStatus,
    Checkpoint,
    CheckpointStatus,
    FailureDuration,
    FailureEnvelope,
    FailureType,
    FailureVisibility,
    PlanDAG,
    PlanStep,
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
                "allow_confirm": self.allow_confirm,
            },
        )


@dataclass(frozen=True, slots=True)
class RuntimeAcceptanceInput:
    """External acceptance facts supplied by tests or future runtime stages."""

    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    passed_tests: tuple[str, ...] = field(default_factory=tuple)
    human_approvals: tuple[str, ...] = field(default_factory=tuple)
    freshness_by_ref: dict[str, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "passed_tests", tuple(self.passed_tests))
        object.__setattr__(self, "human_approvals", tuple(self.human_approvals))
        object.__setattr__(self, "freshness_by_ref", dict(self.freshness_by_ref))


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
    ) -> None:
        self._store = store
        self._tools = tools
        self._acceptance_gate = acceptance_gate or AcceptanceGate()
        self._recovery_policy = recovery_policy or RecoveryPolicy()
        self._resume_policy = resume_policy or ResumePolicy()
        self._step_verifier = step_verifier or StepVerifier()

    async def run(
        self,
        *,
        contract: TaskContract,
        run_id: str,
        steps: tuple[RuntimeToolStep, ...] | list[RuntimeToolStep],
        acceptance: RuntimeAcceptanceInput | None = None,
        model_name: str | None = None,
        prompt_version: str | None = None,
        interactive: bool = True,
    ) -> RuntimeResult:
        if not run_id.strip():
            raise ValueError("run_id must be non-empty")

        acceptance_input = acceptance or RuntimeAcceptanceInput()
        self._validate_linear_step_dependencies(tuple(steps))
        self._store.save_contract(contract)
        run = self._store.create_run(
            TaskRun(
                id=run_id,
                contract_id=contract.id,
                model_name=model_name,
                prompt_version=prompt_version,
            )
        )
        self._append_event(
            run_id=run_id,
            type=TaskEventType.RUN_CREATED,
            payload={"contract_id": contract.id},
        )
        run = self._set_status(run_id, TaskRunStatus.RUNNING)

        evidence_refs: set[str] = set(acceptance_input.evidence_refs)
        tool_results: list[ToolResult] = []

        for step in tuple(steps):
            plan_step = step.to_plan_step()
            self._append_step_started(run_id, plan_step)
            result = await self._tools.execute(
                step.tool_name,
                step.arguments,
                interactive=interactive,
                allow_confirm=step.allow_confirm,
            )
            tool_results.append(result)
            evidence_refs.update(item.ref for item in result.evidence)

            tool_event = self._append_event(
                run_id=run_id,
                type=TaskEventType.TOOL_RESULT_RECORDED,
                payload={
                    "step_id": step.id,
                    "tool_name": step.tool_name,
                    "tool_arguments": dict(step.arguments),
                    "step": self._plan_step_payload(plan_step),
                    "result": result.to_metadata(),
                    "content": result.content,
                    "error": result.error,
                },
                evidence=result.evidence,
                side_effects=result.side_effects,
                idempotency_key=f"tool:{step.id}",
            )

            if not result.ok:
                failure = self._recovery_policy.recommend_from_tool_result(
                    tool_name=step.tool_name,
                    result=result,
                    failed_step=step.id,
                )
                checkpoint = self._create_checkpoint(
                    run_id=run_id,
                    status=self._checkpoint_status_for_run(failure.status),
                    state={
                        "failed_step_id": step.id,
                        "tool_name": step.tool_name,
                    },
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
                run = self._set_status(
                    run_id,
                    failure.status,
                    checkpoint_id=checkpoint.id,
                )
                self._append_terminal_failure_event(run_id, failure)
                return self._result(
                    run_id=run_id,
                    accepted=False,
                    tool_results=tool_results,
                    failure=failure,
                )

            step_failure = self._verify_successful_step(
                run_id=run_id,
                step=step,
                result=result,
                tool_event=tool_event,
                checkpoint_state={
                    "completed_step_id": step.id,
                    "tool_name": step.tool_name,
                },
            )
            if step_failure is not None:
                return self._result(
                    run_id=run_id,
                    accepted=False,
                    tool_results=tool_results,
                    failure=step_failure,
                )

        decision = self._acceptance_gate.evaluate(
            contract,
            evidence_refs=evidence_refs,
            passed_tests=set(acceptance_input.passed_tests),
            human_approvals=set(acceptance_input.human_approvals),
            freshness_by_ref=acceptance_input.freshness_by_ref,
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
            run = self._set_status(
                run_id,
                TaskRunStatus.COMPLETED,
                checkpoint_id=checkpoint.id,
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
            checkpoint = self._create_checkpoint(
                run_id=run_id,
                status=CheckpointStatus.WAITING_USER,
                state={
                    "accepted": False,
                    "blocked_criteria": list(decision.blocked_criteria),
                },
                resume_from_event_id=acceptance_event.id,
            )
            run = self._set_status(
                run_id,
                TaskRunStatus.WAITING_USER,
                checkpoint_id=checkpoint.id,
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
        run = self._set_status(
            run_id,
            TaskRunStatus.ACCEPTANCE_FAILED,
            checkpoint_id=checkpoint.id,
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

    async def resume_from_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
        acceptance: RuntimeAcceptanceInput | None = None,
        interactive: bool = True,
    ) -> RuntimeResult:
        """Resume a failed run from its latest checkpoint when policy allows it."""

        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        contract = self._store.get_contract(run.contract_id)
        if contract is None:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        checkpoint = (
            self._store.get_checkpoint(checkpoint_id)
            if checkpoint_id is not None
            else self._store.latest_checkpoint(run_id)
        )
        if checkpoint is None:
            raise ValueError(f"run has no checkpoint: {run_id}")
        if checkpoint.run_id != run_id:
            raise ValueError("checkpoint does not belong to run")

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

        source_event = self._resume_source_event(run_id, checkpoint)
        source_step = dict(source_event.payload.get("step") or {})
        step = RuntimeToolStep(
            id=str(source_event.payload.get("step_id") or decision.failed_step),
            tool_name=str(source_event.payload.get("tool_name") or ""),
            arguments=dict(source_event.payload.get("tool_arguments") or {}),
            title=source_step.get("title"),
            expected_output=str(source_step.get("expected_output") or ""),
            verification=str(source_step.get("verification") or ""),
            depends_on=tuple(
                str(item)
                for item in source_step.get("depends_on") or ()
            ),
            required_evidence_refs=tuple(
                str(item)
                for item in source_step.get("required_evidence_refs") or ()
            ),
        )
        plan_step = step.to_plan_step()
        self._set_status(run_id, TaskRunStatus.RUNNING, checkpoint_id=checkpoint.id)
        self._append_step_started(
            run_id,
            plan_step,
            payload={
                "resume": {
                    "checkpoint_id": checkpoint.id,
                    "source_event_id": source_event.id,
                    "action": decision.action.value,
                },
            },
        )
        result = await self._tools.execute(
            step.tool_name,
            step.arguments,
            interactive=interactive,
            allow_confirm=step.allow_confirm,
        )
        tool_event = self._append_event(
            run_id=run_id,
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload={
                "step_id": step.id,
                "tool_name": step.tool_name,
                "tool_arguments": dict(step.arguments),
                "step": self._plan_step_payload(plan_step),
                "result": result.to_metadata(),
                "content": result.content,
                "error": result.error,
                "resume": {
                    "checkpoint_id": checkpoint.id,
                    "source_event_id": source_event.id,
                    "action": decision.action.value,
                },
            },
            evidence=result.evidence,
            side_effects=result.side_effects,
            idempotency_key=f"resume:{checkpoint.id}:tool:{step.id}",
        )
        if not result.ok:
            failure = self._recovery_policy.recommend_from_tool_result(
                tool_name=step.tool_name,
                result=result,
                failed_step=step.id,
            )
            new_checkpoint = self._create_checkpoint(
                run_id=run_id,
                status=self._checkpoint_status_for_run(failure.status),
                state={
                    "failed_step_id": step.id,
                    "tool_name": step.tool_name,
                    "retry_of_checkpoint_id": checkpoint.id,
                },
                resume_from_event_id=tool_event.id,
                failure=failure,
            )
            self._append_step_verified(
                run_id,
                self._step_verification_from_failure(
                    plan_step=plan_step,
                    failure=failure,
                    result=result,
                    checkpoint_id=new_checkpoint.id,
                ),
                plan_step=plan_step,
            )
            self._set_status(run_id, failure.status, checkpoint_id=new_checkpoint.id)
            self._append_terminal_failure_event(run_id, failure)
            return self._result(run_id=run_id, accepted=False, tool_results=[result], failure=failure)

        step_failure = self._verify_successful_step(
            run_id=run_id,
            step=step,
            result=result,
            tool_event=tool_event,
            checkpoint_state={
                "completed_step_id": step.id,
                "tool_name": step.tool_name,
                "retry_of_checkpoint_id": checkpoint.id,
            },
        )
        if step_failure is not None:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[result],
                failure=step_failure,
            )
        return self._finish_acceptance(
            run_id=run_id,
            contract=contract,
            acceptance=acceptance or RuntimeAcceptanceInput(),
            tool_results=[result],
        )

    async def resume_with_alternative_tool(
        self,
        *,
        run_id: str,
        alternative_step: RuntimeToolStep,
        checkpoint_id: str | None = None,
        acceptance: RuntimeAcceptanceInput | None = None,
        interactive: bool = True,
    ) -> RuntimeResult:
        """Resume a checkpoint with an explicit alternative tool step."""

        self._validate_linear_step_dependencies((alternative_step,))
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        contract = self._store.get_contract(run.contract_id)
        if contract is None:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        checkpoint = (
            self._store.get_checkpoint(checkpoint_id)
            if checkpoint_id is not None
            else self._store.latest_checkpoint(run_id)
        )
        if checkpoint is None:
            raise ValueError(f"run has no checkpoint: {run_id}")
        if checkpoint.run_id != run_id:
            raise ValueError("checkpoint does not belong to run")
        failure = checkpoint.failure
        if failure is None:
            raise ValueError("checkpoint has no failure envelope")
        if failure.recommended_action is not RecoveryAction.USE_ALTERNATIVE_TOOL:
            raise ValueError("checkpoint is not waiting for alternative tool")

        source_event = self._resume_source_event(run_id, checkpoint)
        plan_step = alternative_step.to_plan_step()
        self._set_status(run_id, TaskRunStatus.RUNNING, checkpoint_id=checkpoint.id)
        self._append_step_started(
            run_id,
            plan_step,
            payload={
                "alternative": {
                    "checkpoint_id": checkpoint.id,
                    "source_event_id": source_event.id,
                    "failed_step": failure.failed_step,
                    "replacement_for_tool": failure.metadata.get("tool_name"),
                },
            },
        )
        result = await self._tools.execute(
            alternative_step.tool_name,
            alternative_step.arguments,
            interactive=interactive,
            allow_confirm=alternative_step.allow_confirm,
        )
        tool_event = self._append_event(
            run_id=run_id,
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload={
                "step_id": alternative_step.id,
                "tool_name": alternative_step.tool_name,
                "tool_arguments": dict(alternative_step.arguments),
                "step": self._plan_step_payload(plan_step),
                "result": result.to_metadata(),
                "content": result.content,
                "error": result.error,
                "alternative": {
                    "checkpoint_id": checkpoint.id,
                    "source_event_id": source_event.id,
                    "failed_step": failure.failed_step,
                    "replacement_for_tool": failure.metadata.get("tool_name"),
                },
            },
            evidence=result.evidence,
            side_effects=result.side_effects,
            idempotency_key=f"alternative:{checkpoint.id}:tool:{alternative_step.id}",
        )
        if not result.ok:
            new_failure = self._recovery_policy.recommend_from_tool_result(
                tool_name=alternative_step.tool_name,
                result=result,
                failed_step=alternative_step.id,
            )
            new_checkpoint = self._create_checkpoint(
                run_id=run_id,
                status=self._checkpoint_status_for_run(new_failure.status),
                state={
                    "failed_step_id": alternative_step.id,
                    "tool_name": alternative_step.tool_name,
                    "alternative_of_checkpoint_id": checkpoint.id,
                },
                resume_from_event_id=tool_event.id,
                failure=new_failure,
            )
            self._append_step_verified(
                run_id,
                self._step_verification_from_failure(
                    plan_step=plan_step,
                    failure=new_failure,
                    result=result,
                    checkpoint_id=new_checkpoint.id,
                ),
                plan_step=plan_step,
            )
            self._set_status(
                run_id,
                new_failure.status,
                checkpoint_id=new_checkpoint.id,
            )
            self._append_terminal_failure_event(run_id, new_failure)
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[result],
                failure=new_failure,
            )

        step_failure = self._verify_successful_step(
            run_id=run_id,
            step=alternative_step,
            result=result,
            tool_event=tool_event,
            checkpoint_state={
                "completed_step_id": alternative_step.id,
                "tool_name": alternative_step.tool_name,
                "alternative_of_checkpoint_id": checkpoint.id,
            },
        )
        if step_failure is not None:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[result],
                failure=step_failure,
            )
        return self._finish_acceptance(
            run_id=run_id,
            contract=contract,
            acceptance=acceptance or RuntimeAcceptanceInput(),
            tool_results=[result],
        )

    async def resume_with_user_approval(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
        feedback: str = "",
        acceptance: RuntimeAcceptanceInput | None = None,
        interactive: bool = True,
    ) -> RuntimeResult:
        """Resume an ask-user checkpoint after explicit user approval."""

        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        contract = self._store.get_contract(run.contract_id)
        if contract is None:
            raise ValueError(f"unknown contract id: {run.contract_id}")
        checkpoint = (
            self._store.get_checkpoint(checkpoint_id)
            if checkpoint_id is not None
            else self._store.latest_checkpoint(run_id)
        )
        if checkpoint is None:
            raise ValueError(f"run has no checkpoint: {run_id}")
        if checkpoint.run_id != run_id:
            raise ValueError("checkpoint does not belong to run")
        failure = checkpoint.failure
        if failure is None:
            raise ValueError("checkpoint has no failure envelope")
        if failure.recommended_action is not RecoveryAction.ASK_USER:
            raise ValueError("checkpoint is not waiting for user approval")

        source_event = self._resume_source_event(run_id, checkpoint)
        source_step = dict(source_event.payload.get("step") or {})
        step = RuntimeToolStep(
            id=str(source_event.payload.get("step_id") or failure.failed_step),
            tool_name=str(source_event.payload.get("tool_name") or ""),
            arguments=dict(source_event.payload.get("tool_arguments") or {}),
            allow_confirm=True,
            title=source_step.get("title"),
            expected_output=str(source_step.get("expected_output") or ""),
            verification=str(source_step.get("verification") or ""),
            depends_on=tuple(
                str(item)
                for item in source_step.get("depends_on") or ()
            ),
            required_evidence_refs=tuple(
                str(item)
                for item in source_step.get("required_evidence_refs") or ()
            ),
        )
        plan_step = step.to_plan_step()
        self._append_event(
            run_id=run_id,
            type=TaskEventType.USER_INPUT_RECORDED,
            payload={
                "checkpoint_id": checkpoint.id,
                "approved": True,
                "feedback": feedback,
                "failed_step": failure.failed_step,
            },
        )
        self._set_status(run_id, TaskRunStatus.RUNNING, checkpoint_id=checkpoint.id)
        self._append_step_started(
            run_id,
            plan_step,
            payload={
                "user_approval": {
                    "checkpoint_id": checkpoint.id,
                    "source_event_id": source_event.id,
                },
            },
        )
        result = await self._tools.execute(
            step.tool_name,
            step.arguments,
            interactive=interactive,
            allow_confirm=True,
        )
        tool_event = self._append_event(
            run_id=run_id,
            type=TaskEventType.TOOL_RESULT_RECORDED,
            payload={
                "step_id": step.id,
                "tool_name": step.tool_name,
                "tool_arguments": dict(step.arguments),
                "step": self._plan_step_payload(plan_step),
                "result": result.to_metadata(),
                "content": result.content,
                "error": result.error,
                "user_approval": {
                    "checkpoint_id": checkpoint.id,
                    "source_event_id": source_event.id,
                },
            },
            evidence=result.evidence,
            side_effects=result.side_effects,
            idempotency_key=f"approval:{checkpoint.id}:tool:{step.id}",
        )
        if not result.ok:
            new_failure = self._recovery_policy.recommend_from_tool_result(
                tool_name=step.tool_name,
                result=result,
                failed_step=step.id,
            )
            new_checkpoint = self._create_checkpoint(
                run_id=run_id,
                status=self._checkpoint_status_for_run(new_failure.status),
                state={
                    "failed_step_id": step.id,
                    "tool_name": step.tool_name,
                    "approval_of_checkpoint_id": checkpoint.id,
                },
                resume_from_event_id=tool_event.id,
                failure=new_failure,
            )
            self._append_step_verified(
                run_id,
                self._step_verification_from_failure(
                    plan_step=plan_step,
                    failure=new_failure,
                    result=result,
                    checkpoint_id=new_checkpoint.id,
                ),
                plan_step=plan_step,
            )
            self._set_status(run_id, new_failure.status, checkpoint_id=new_checkpoint.id)
            self._append_terminal_failure_event(run_id, new_failure)
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[result],
                failure=new_failure,
            )

        step_failure = self._verify_successful_step(
            run_id=run_id,
            step=step,
            result=result,
            tool_event=tool_event,
            checkpoint_state={
                "completed_step_id": step.id,
                "tool_name": step.tool_name,
                "approval_of_checkpoint_id": checkpoint.id,
            },
        )
        if step_failure is not None:
            return self._result(
                run_id=run_id,
                accepted=False,
                tool_results=[result],
                failure=step_failure,
            )
        return self._finish_acceptance(
            run_id=run_id,
            contract=contract,
            acceptance=acceptance or RuntimeAcceptanceInput(),
            tool_results=[result],
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
                status=CheckpointStatus.FAILED,
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
            self._set_status(run_id, TaskRunStatus.FAILED, checkpoint_id=checkpoint.id)
            self._append_terminal_failure_event(run_id, failure)
            return failure

        checkpoint = self._create_checkpoint(
            run_id=run_id,
            status=CheckpointStatus.RUNNING,
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
        updated = self._store.update_run(
            current.with_status(status, checkpoint_id=checkpoint_id)
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
        acceptance: RuntimeAcceptanceInput,
        tool_results: list[ToolResult],
    ) -> RuntimeResult:
        evidence_refs = set(acceptance.evidence_refs)
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
            passed_tests=set(acceptance.passed_tests),
            human_approvals=set(acceptance.human_approvals),
            freshness_by_ref=acceptance.freshness_by_ref,
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
            self._set_status(run_id, TaskRunStatus.COMPLETED, checkpoint_id=checkpoint.id)
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
            checkpoint = self._create_checkpoint(
                run_id=run_id,
                status=CheckpointStatus.WAITING_USER,
                state={
                    "accepted": False,
                    "blocked_criteria": list(decision.blocked_criteria),
                },
                resume_from_event_id=acceptance_event.id,
            )
            self._set_status(run_id, TaskRunStatus.WAITING_USER, checkpoint_id=checkpoint.id)
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
        self._set_status(run_id, TaskRunStatus.ACCEPTANCE_FAILED, checkpoint_id=checkpoint.id)
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
