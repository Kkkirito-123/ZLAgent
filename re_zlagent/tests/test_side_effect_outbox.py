from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.runtime import (  # noqa: E402
    HarnessRuntime,
    InjectedOutboxCrash,
    OutboxAction,
    OutboxFaultPoint,
    SideEffectOutbox,
    SideEffectReconciler,
    RuntimeToolStep,
)
from re_zlagent.harness.storage import (  # noqa: E402
    InMemoryLongTaskStore,
    InMemoryTaskStore,
    LongTaskStore,
    SqliteLongTaskStore,
    SqliteTaskStore,
)
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    SideEffectStatus,
    TaskContract,
    TaskRunStatus,
)
from re_zlagent.harness.tools import (  # noqa: E402
    Evidence,
    SideEffect,
    Tool,
    ToolExecutionContext,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)
from re_zlagent.harness.tools.builtins import create_file_tools  # noqa: E402


class CrashAt:
    def __init__(self, point: OutboxFaultPoint) -> None:
        self.point = point

    def hit(self, point: OutboxFaultPoint, records: tuple) -> None:
        if point is self.point:
            raise InjectedOutboxCrash(point.value)


class DeduplicatingAdapter:
    def __init__(self) -> None:
        self.dispatch_calls = 0
        self.applied_keys: set[str] = set()

    def apply(self, idempotency_key: str) -> None:
        self.dispatch_calls += 1
        self.applied_keys.add(idempotency_key)


class ExternalActionTool(Tool):
    name = "external_action"
    description = "Apply one externally visible test action."
    permission = ToolPermission.SAFE
    is_read_only = False
    side_effects = ("external",)
    outbox_required = True

    def __init__(
        self,
        adapter: DeduplicatingAdapter,
        *,
        retry_safe: bool,
        result_target: str = "remote:item-1",
    ) -> None:
        self._adapter = adapter
        self.side_effect_retry_safe = retry_safe
        self._result_target = result_target

    def plan_side_effects(
        self,
        arguments: dict[str, Any],
    ) -> tuple[SideEffect, ...]:
        return (
            SideEffect(
                type="external",
                target="remote:item-1",
                risk="high",
            ),
        )

    async def execute_with_context(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        key = context.side_effect_keys[0]
        self._adapter.apply(key)
        return ToolResult.success(
            "applied",
            evidence=[Evidence(type="external", ref="evidence:external")],
            side_effects=[
                SideEffect(
                    type="external",
                    target=self._result_target,
                    risk="high",
                )
            ],
            source=self.name,
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raise AssertionError("runtime must use execute_with_context")


class UndeclaredSideEffectTool(Tool):
    name = "undeclared_action"
    description = "Violate the side-effect declaration contract."
    permission = ToolPermission.SAFE
    is_read_only = False

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success(
            "unexpected mutation",
            evidence=[Evidence(type="external", ref="evidence:external")],
            side_effects=[
                SideEffect(type="external", target="remote:undeclared")
            ],
            source=self.name,
        )


class SideEffectOutboxTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _contract() -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="apply one external action",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="external-evidence",
                    description="external action produced evidence",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("evidence:external",),
                ),
            ),
        )

    @staticmethod
    def _registry(tool: Tool) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(tool)
        return registry

    async def _recover_outbox(
        self,
        *,
        store: LongTaskStore,
        registry: ToolRegistry,
    ) -> tuple[ToolResult, SideEffectStatus]:
        prepared_call = registry.prepare(
            "external_action",
            {},
            idempotency_key=(
                "run:run-1:plan:plan_run-1:step:step-1"
            ),
        )
        outbox = SideEffectOutbox(store)
        preparation = outbox.prepare(
            run_id="run-1",
            plan_id="plan_run-1",
            step_id="step-1",
            call=prepared_call,
        )
        if preparation.action is OutboxAction.DISPATCH:
            preparation = outbox.begin_dispatch(preparation)
            result = await registry.execute_prepared(prepared_call)
            preparation, result = outbox.record_result(
                preparation,
                result,
                expected_intents=prepared_call.side_effect_intents,
            )
        elif preparation.action is OutboxAction.REPLAY_RESULT:
            result = preparation.result
        else:
            result = preparation.result
        if result is None:
            raise AssertionError("outbox recovery returned no result")
        preparation = outbox.confirm_result(
            preparation,
            result_event_id="evt-recovered",
        )
        return result, preparation.records[0].status

    async def test_all_crash_points_recover_without_duplicate_external_effect(self) -> None:
        expected_status = {
            OutboxFaultPoint.AFTER_INTENT_PERSISTED: SideEffectStatus.PLANNED,
            OutboxFaultPoint.AFTER_DISPATCH: SideEffectStatus.DISPATCHING,
            OutboxFaultPoint.BEFORE_RESULT_COMMIT: SideEffectStatus.APPLIED,
        }
        expected_dispatch_calls = {
            OutboxFaultPoint.AFTER_INTENT_PERSISTED: 1,
            OutboxFaultPoint.AFTER_DISPATCH: 2,
            OutboxFaultPoint.BEFORE_RESULT_COMMIT: 1,
        }

        for point in OutboxFaultPoint:
            with self.subTest(point=point.value):
                task_store = InMemoryTaskStore()
                long_task_store = InMemoryLongTaskStore()
                adapter = DeduplicatingAdapter()
                registry = self._registry(
                    ExternalActionTool(adapter, retry_safe=True)
                )
                runtime = HarnessRuntime(
                    store=task_store,
                    tools=registry,
                    long_task_store=long_task_store,
                    outbox_fault_injector=CrashAt(point),
                )

                with self.assertRaises(InjectedOutboxCrash):
                    await runtime.run(
                        contract=self._contract(),
                        run_id="run-1",
                        steps=[
                            RuntimeToolStep(
                                id="step-1",
                                tool_name="external_action",
                            )
                        ],
                    )

                record = long_task_store.list_side_effects("run-1")[0]
                report = SideEffectReconciler(long_task_store).inspect("run-1")
                self.assertEqual(record.status, expected_status[point])
                if point is OutboxFaultPoint.BEFORE_RESULT_COMMIT:
                    self.assertEqual(report.pending_result_commit_ids, (record.id,))
                else:
                    self.assertEqual(report.retryable_ids, (record.id,))

                resumed = await HarnessRuntime(
                    store=task_store,
                    tools=registry,
                    long_task_store=long_task_store,
                ).resume_incomplete_run(
                    run_id="run-1",
                )
                recovered_status = long_task_store.list_side_effects(
                    "run-1"
                )[0].status

                self.assertTrue(resumed.accepted)
                self.assertEqual(recovered_status, SideEffectStatus.CONFIRMED)
                self.assertEqual(adapter.dispatch_calls, expected_dispatch_calls[point])
                self.assertEqual(len(adapter.applied_keys), 1)

    async def test_non_idempotent_interrupted_dispatch_requires_manual_reconciliation(
        self,
    ) -> None:
        task_store = InMemoryTaskStore()
        long_task_store = InMemoryLongTaskStore()
        adapter = DeduplicatingAdapter()
        registry = self._registry(
            ExternalActionTool(adapter, retry_safe=False)
        )
        runtime = HarnessRuntime(
            store=task_store,
            tools=registry,
            long_task_store=long_task_store,
            outbox_fault_injector=CrashAt(OutboxFaultPoint.AFTER_DISPATCH),
        )

        with self.assertRaises(InjectedOutboxCrash):
            await runtime.run(
                contract=self._contract(),
                run_id="run-1",
                steps=[RuntimeToolStep(id="step-1", tool_name="external_action")],
            )

        prepared_call = registry.prepare(
            "external_action",
            {},
            idempotency_key="run:run-1:plan:plan_run-1:step:step-1",
        )
        preparation = SideEffectOutbox(long_task_store).prepare(
            run_id="run-1",
            plan_id="plan_run-1",
            step_id="step-1",
            call=prepared_call,
        )
        record = long_task_store.list_side_effects("run-1")[0]

        self.assertEqual(preparation.action, OutboxAction.BLOCK)
        self.assertEqual(record.status, SideEffectStatus.UNCERTAIN)
        self.assertEqual(adapter.dispatch_calls, 1)
        report = SideEffectReconciler(long_task_store).inspect("run-1")
        self.assertEqual(report.manual_review_ids, (record.id,))

        reconciled_result = ToolResult.success(
            "verified applied",
            evidence=[Evidence(type="external", ref="evidence:external")],
            side_effects=[
                SideEffect(
                    type="external",
                    target="remote:item-1",
                    risk="high",
                )
            ],
            source="external_action",
        )
        resolved = SideEffectReconciler(long_task_store).resolve_uncertain(
            record.id,
            status=SideEffectStatus.CONFIRMED,
            note="verified external audit log",
            result=reconciled_result,
        )
        self.assertEqual(resolved.status, SideEffectStatus.CONFIRMED)
        replay = SideEffectOutbox(long_task_store).prepare(
            run_id="run-1",
            plan_id="plan_run-1",
            step_id="step-1",
            call=prepared_call,
        )
        self.assertEqual(replay.action, OutboxAction.REPLAY_RESULT)
        self.assertTrue(replay.result.ok)

    async def test_applied_result_replays_after_sqlite_reopen_without_redispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "outbox.sqlite"
            task_store = SqliteTaskStore(path)
            long_task_store = SqliteLongTaskStore(path)
            adapter = DeduplicatingAdapter()
            registry = self._registry(
                ExternalActionTool(adapter, retry_safe=True)
            )
            runtime = HarnessRuntime(
                store=task_store,
                tools=registry,
                long_task_store=long_task_store,
                outbox_fault_injector=CrashAt(
                    OutboxFaultPoint.BEFORE_RESULT_COMMIT
                ),
            )

            with self.assertRaises(InjectedOutboxCrash):
                await runtime.run(
                    contract=self._contract(),
                    run_id="run-1",
                    steps=[
                        RuntimeToolStep(
                            id="step-1",
                            tool_name="external_action",
                        )
                    ],
                )
            task_store.close()
            long_task_store.close()

            reopened = SqliteLongTaskStore(path)
            result, status = await self._recover_outbox(
                store=reopened,
                registry=registry,
            )

            self.assertTrue(result.ok)
            self.assertEqual(status, SideEffectStatus.CONFIRMED)
            self.assertEqual(adapter.dispatch_calls, 1)
            self.assertEqual(len(adapter.applied_keys), 1)
            reopened.close()

    async def test_missing_outbox_store_blocks_before_external_dispatch(self) -> None:
        task_store = InMemoryTaskStore()
        adapter = DeduplicatingAdapter()
        runtime = HarnessRuntime(
            store=task_store,
            tools=self._registry(ExternalActionTool(adapter, retry_safe=True)),
        )

        result = await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="external_action")],
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.FAILED)
        self.assertEqual(result.failure.recommended_action.value, "manual_review")
        self.assertEqual(adapter.dispatch_calls, 0)

    async def test_tool_intent_mismatch_is_recorded_as_uncertain(self) -> None:
        long_task_store = InMemoryLongTaskStore()
        adapter = DeduplicatingAdapter()
        runtime = HarnessRuntime(
            store=InMemoryTaskStore(),
            tools=self._registry(
                ExternalActionTool(
                    adapter,
                    retry_safe=True,
                    result_target="remote:different",
                )
            ),
            long_task_store=long_task_store,
        )

        result = await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[RuntimeToolStep(id="step-1", tool_name="external_action")],
        )

        record = long_task_store.list_side_effects("run-1")[0]
        self.assertFalse(result.accepted)
        self.assertEqual(record.status, SideEffectStatus.UNCERTAIN)
        self.assertEqual(result.failure.recommended_action.value, "manual_review")
        self.assertEqual(adapter.dispatch_calls, 1)

    async def test_file_write_uses_content_hash_for_idempotent_crash_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            task_store = InMemoryTaskStore()
            long_task_store = InMemoryLongTaskStore()
            _, write_tool = create_file_tools(workspace)
            registry = self._registry(write_tool)
            runtime = HarnessRuntime(
                store=task_store,
                tools=registry,
                long_task_store=long_task_store,
                outbox_fault_injector=CrashAt(OutboxFaultPoint.AFTER_DISPATCH),
            )
            arguments = {"path": "result.txt", "content": "stable content"}
            contract = TaskContract(
                id="contract-file",
                user_goal="write a crash-safe file",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        id="file-evidence",
                        description="file evidence exists",
                        type=CriterionType.TOOL_EVIDENCE,
                        evidence_refs=("result.txt",),
                    ),
                ),
            )

            with self.assertRaises(InjectedOutboxCrash):
                await runtime.run(
                    contract=contract,
                    run_id="run-file",
                    steps=[
                        RuntimeToolStep(
                            id="write",
                            tool_name="write_file",
                            arguments=arguments,
                            allow_confirm=True,
                        )
                    ],
                )

            self.assertEqual((workspace / "result.txt").read_text(), "stable content")
            prepared_call = registry.prepare(
                "write_file",
                arguments,
                allow_confirm=True,
                idempotency_key=(
                    "run:run-file:plan:plan_run-file:step:write"
                ),
            )
            outbox = SideEffectOutbox(long_task_store)
            preparation = outbox.prepare(
                run_id="run-file",
                plan_id="plan_run-file",
                step_id="write",
                call=prepared_call,
            )
            preparation = outbox.begin_dispatch(preparation)
            result = await registry.execute_prepared(prepared_call)
            preparation, result = outbox.record_result(
                preparation,
                result,
                expected_intents=prepared_call.side_effect_intents,
            )
            preparation = outbox.confirm_result(
                preparation,
                result_event_id="evt-file-recovered",
            )

            self.assertTrue(result.ok)
            self.assertTrue(result.raw["idempotent_replay"])
            self.assertEqual(
                preparation.records[0].status,
                SideEffectStatus.CONFIRMED,
            )
            self.assertEqual((workspace / "result.txt").read_text(), "stable content")

    async def test_undeclared_side_effect_is_quarantined_as_uncertain(self) -> None:
        long_task_store = InMemoryLongTaskStore()
        runtime = HarnessRuntime(
            store=InMemoryTaskStore(),
            tools=self._registry(UndeclaredSideEffectTool()),
            long_task_store=long_task_store,
        )

        result = await runtime.run(
            contract=self._contract(),
            run_id="run-1",
            steps=[
                RuntimeToolStep(
                    id="step-1",
                    tool_name="undeclared_action",
                )
            ],
        )

        record = long_task_store.list_side_effects("run-1")[0]
        self.assertFalse(result.accepted)
        self.assertEqual(result.run.status, TaskRunStatus.FAILED)
        self.assertEqual(record.status, SideEffectStatus.UNCERTAIN)
        self.assertIn("not declared", record.metadata["uncertain_reason"])
        self.assertEqual(result.failure.recommended_action.value, "manual_review")


if __name__ == "__main__":
    unittest.main()
