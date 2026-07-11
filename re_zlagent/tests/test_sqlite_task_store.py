from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.storage import SqliteTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CheckpointStatus,
    CriterionType,
    FailureEnvelope,
    FailureType,
    PlanDAG,
    PlanStep,
    ProgramPlan,
    RecoveryAction,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import Evidence  # noqa: E402


class SqliteTaskStoreTests(unittest.TestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="persist task",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="tests",
                    description="tests pass",
                    type=CriterionType.TEST_RESULT,
                ),
            ),
        )

    def _plan(self, *, title: str = "execute") -> ProgramPlan:
        return ProgramPlan(
            id="plan-1",
            contract_id="contract-1",
            dag=PlanDAG((PlanStep(id="step-1", title=title),)),
        )

    def test_contract_save_is_idempotent_but_identity_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")
            contract = self._contract()

            first = store.save_contract(contract)
            replay = store.save_contract(contract)
            store.create_run(TaskRun(id="run-1", contract_id=contract.id))

            self.assertEqual(replay, first)
            with self.assertRaises(ValueError):
                store.save_contract(replace(contract, user_goal="changed goal"))
            self.assertEqual(
                store.get_contract(store.get_run("run-1").contract_id),
                contract,
            )
            store.close()

    def test_plan_survives_reopen_and_identity_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.sqlite"
            store = SqliteTaskStore(db_path)
            store.save_contract(self._contract())
            plan = self._plan()
            store.save_plan(plan)
            store.save_plan(plan)
            store.create_run(
                TaskRun(id="run-1", contract_id="contract-1", plan_id=plan.id)
            )
            store.close()

            reopened = SqliteTaskStore(db_path)
            self.assertEqual(reopened.get_plan(plan.id), plan)
            self.assertEqual(reopened.get_run("run-1").plan_id, plan.id)
            with self.assertRaises(ValueError):
                reopened.save_plan(self._plan(title="changed"))
            reopened.close()

    def test_run_plan_contract_binding_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")
            contract = self._contract()
            other_contract = replace(contract, id="contract-2")
            store.save_contract(contract)
            store.save_contract(other_contract)
            plan = self._plan()
            other_plan = replace(plan, id="plan-2", contract_id="contract-2")
            store.save_plan(plan)
            store.save_plan(other_plan)

            with self.assertRaises(ValueError):
                store.create_run(
                    TaskRun(
                        id="cross-boundary",
                        contract_id="contract-2",
                        plan_id=plan.id,
                    )
                )

            run = store.create_run(
                TaskRun(id="run-1", contract_id=contract.id, plan_id=plan.id)
            )
            with self.assertRaises(ValueError):
                store.update_run(replace(run, contract_id="contract-2"))
            with self.assertRaises(ValueError):
                store.update_run(replace(run, plan_id=other_plan.id))
            store.close()

    def test_persists_contract_run_event_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.sqlite"
            store = SqliteTaskStore(db_path)
            store.save_contract(self._contract())
            store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
            event = store.append_event(
                run_id="run-1",
                type=TaskEventType.RUN_CREATED,
                evidence=[Evidence(type="contract", ref="contract-1")],
            )
            checkpoint = store.create_checkpoint(
                run_id="run-1",
                status=CheckpointStatus.RUNNING,
                resume_from_event_id=event.id,
            )
            store.close()

            reopened = SqliteTaskStore(db_path)
            self.assertEqual(reopened.get_contract("contract-1").user_goal, "persist task")
            self.assertEqual(reopened.get_run("run-1").event_seq, 1)
            self.assertEqual(reopened.list_events("run-1")[0].evidence[0].ref, "contract-1")
            self.assertEqual(reopened.latest_checkpoint("run-1").id, checkpoint.id)
            reopened.close()

    def test_idempotency_key_returns_existing_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")
            store.save_contract(self._contract())
            store.create_run(TaskRun(id="run-1", contract_id="contract-1"))

            first = store.append_event(
                run_id="run-1",
                type=TaskEventType.TOOL_RESULT_RECORDED,
                payload={"value": 1},
                idempotency_key="tool-1",
            )
            retry = store.append_event(
                run_id="run-1",
                type=TaskEventType.TOOL_RESULT_RECORDED,
                payload={"value": 2},
                idempotency_key="tool-1",
            )

            self.assertEqual(first.id, retry.id)
            self.assertEqual(retry.payload, {"value": 1})
            self.assertEqual(len(store.list_events("run-1")), 1)
            store.close()

    def test_update_run_does_not_decrease_event_seq(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")
            store.save_contract(self._contract())
            store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
            store.append_event(run_id="run-1", type=TaskEventType.RUN_CREATED)

            updated = store.update_run(
                TaskRun(
                    id="run-1",
                    contract_id="contract-1",
                    status=TaskRunStatus.RUNNING,
                    event_seq=0,
                )
            )

            self.assertEqual(updated.event_seq, 1)
            self.assertEqual(store.get_run("run-1").event_seq, 1)
            store.close()

    def test_checkpoint_failure_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")
            store.save_contract(self._contract())
            store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
            failure = FailureEnvelope(
                status=TaskRunStatus.RECOVERING,
                failed_step="step-1",
                failure_type=FailureType.TOOL_ERROR,
                root_cause="failed",
                recoverable=True,
                recommended_action=RecoveryAction.RETRY,
            )

            checkpoint = store.create_checkpoint(
                run_id="run-1",
                status=CheckpointStatus.FAILED,
                failure=failure,
            )

            loaded = store.get_checkpoint(checkpoint.id)
            self.assertEqual(loaded.failure.failed_step, "step-1")
            self.assertEqual(loaded.failure.recommended_action, RecoveryAction.RETRY)
            store.close()

    def test_unknown_run_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SqliteTaskStore(Path(tmp) / "tasks.sqlite")

            with self.assertRaises(ValueError):
                store.append_event(run_id="missing", type=TaskEventType.RUN_CREATED)
            with self.assertRaises(ValueError):
                store.create_checkpoint(
                    run_id="missing",
                    status=CheckpointStatus.RUNNING,
                )
            store.close()


if __name__ == "__main__":
    unittest.main()
