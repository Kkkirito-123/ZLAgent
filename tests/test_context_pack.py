from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.runtime import ContextPackBuilder  # noqa: E402
from re_zlagent.harness.storage import InMemoryLongTaskStore, InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    ArtifactKind,
    ArtifactRecord,
    CheckpointStatus,
    CriterionType,
    DagExecutionPolicy,
    PendingInteraction,
    PlanDAG,
    PlanStep,
    ProgramPlan,
    StepStatus,
    TaskContract,
    TaskEventType,
    TaskRun,
    TaskRunStatus,
)
from re_zlagent.harness.tools import Evidence  # noqa: E402


class ContextPackBuilderTests(unittest.TestCase):
    def _contract(self) -> TaskContract:
        return TaskContract(
            id="contract-1",
            user_goal="resume a long task",
            acceptance_criteria=(
                AcceptanceCriterion(
                    id="has-collect-evidence",
                    description="collect evidence exists",
                    type=CriterionType.TOOL_EVIDENCE,
                    evidence_refs=("evidence:collect",),
                ),
                AcceptanceCriterion(
                    id="operator-approval",
                    description="operator approves resume",
                    type=CriterionType.HUMAN_APPROVAL,
                ),
            ),
        )

    def _program_plan(self) -> ProgramPlan:
        return ProgramPlan(
            id="program-1",
            contract_id="contract-1",
            dag=PlanDAG((
                PlanStep(id="collect", title="Collect context"),
                PlanStep(id="ask", title="Ask user", depends_on=("collect",)),
            )),
        )

    def _store(self) -> InMemoryTaskStore:
        store = InMemoryTaskStore()
        store.save_contract(self._contract())
        store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
        store.append_event(
            run_id="run-1",
            type=TaskEventType.PLAN_STEP_STARTED,
            payload={"step_id": "collect", "status": StepStatus.RUNNING.value},
        )
        store.append_event(
            run_id="run-1",
            type=TaskEventType.PLAN_STEP_VERIFIED,
            payload={
                "step_id": "collect",
                "status": StepStatus.PASSED.value,
                "evidence_refs": ["evidence:collect"],
                "checkpoint_id": "chk-collect",
            },
            evidence=[
                Evidence(
                    type="runtime",
                    ref="evidence:collect",
                    summary="collect evidence",
                )
            ],
        )
        checkpoint = store.create_checkpoint(
            run_id="run-1",
            status=CheckpointStatus.WAITING_USER,
            state={"waiting_for": "operator-approval"},
            checkpoint_id="chk-wait",
        )
        store.update_run(
            TaskRun(
                id="run-1",
                contract_id="contract-1",
                status=TaskRunStatus.WAITING_USER,
                current_checkpoint_id=checkpoint.id,
            )
        )
        store.append_event(
            run_id="run-1",
            type=TaskEventType.USER_INPUT_REQUIRED,
            payload={
                "checkpoint_id": "chk-wait",
                "step_id": "ask",
                "reason": "operator approval required",
            },
        )
        store.append_event(
            run_id="run-1",
            type=TaskEventType.ACCEPTANCE_EVALUATED,
            payload={
                "accepted": False,
                "status": "blocked",
                "reason": "operator approval required",
                "passed_criteria": ["has-collect-evidence"],
                "failed_criteria": [],
                "blocked_criteria": ["operator-approval"],
            },
        )
        return store

    def test_context_pack_preserves_resume_facts_without_full_history(self) -> None:
        pending = PendingInteraction(
            id="pi-1",
            run_id="run-1",
            checkpoint_id="chk-wait",
            question="Approve resume?",
            required_by_step_id="ask",
            resume_token="resume-token-1",
        )
        artifact = ArtifactRecord(
            id="artifact-1",
            run_id="run-1",
            ref="artifact://collect-summary",
            kind=ArtifactKind.REPORT,
            producer_step_id="collect",
            evidence_refs=("evidence:collect",),
        )

        pack = ContextPackBuilder(self._store()).build(
            run_id="run-1",
            program_plan=self._program_plan(),
            pending_interactions=(pending,),
            artifact_records=(artifact,),
            max_recent_events=2,
        )

        self.assertEqual(pack.run_id, "run-1")
        self.assertEqual(pack.checkpoint_id, "chk-wait")
        self.assertEqual(pack.checkpoint_status, "waiting_user")
        self.assertEqual(pack.completed_step_ids, ("collect",))
        self.assertEqual(pack.frontier_step_ids, ())
        self.assertEqual(pack.blocked_step_ids, ("ask",))
        self.assertEqual(pack.evidence_refs, ("evidence:collect",))
        self.assertEqual(pack.acceptance.accepted, False)
        self.assertEqual(pack.acceptance.blocked_criteria, ("operator-approval",))
        self.assertEqual(pack.pending_interactions[0]["resume_token"], "resume-token-1")
        self.assertEqual(pack.artifact_records[0]["ref"], "artifact://collect-summary")
        self.assertEqual(len(pack.recent_events), 2)
        self.assertEqual(pack.metadata["run_status"], "waiting_user")

    def test_context_pack_can_load_long_task_records_from_store(self) -> None:
        long_task_store = InMemoryLongTaskStore()
        pending = long_task_store.save_pending_interaction(
            PendingInteraction(
                id="pi-1",
                run_id="run-1",
                checkpoint_id="chk-wait",
                question="Approve resume?",
                required_by_step_id="ask",
                resume_token="resume-token-1",
            )
        )
        long_task_store.save_artifact(
            ArtifactRecord(
                id="artifact-1",
                run_id="run-1",
                ref="artifact://collect-summary",
                kind=ArtifactKind.REPORT,
                producer_step_id="collect",
                evidence_refs=("evidence:collect",),
            )
        )

        pack = ContextPackBuilder(
            self._store(),
            long_task_store=long_task_store,
        ).build(
            run_id="run-1",
            program_plan=self._program_plan(),
        )

        self.assertEqual(pack.pending_interactions[0]["id"], pending.id)
        self.assertEqual(pack.pending_interactions[0]["resume_token"], "resume-token-1")
        self.assertEqual(pack.artifact_records[0]["id"], "artifact-1")

    def test_context_pack_can_include_dag_execution_assessment(self) -> None:
        store = InMemoryTaskStore()
        store.save_contract(self._contract())
        store.create_run(TaskRun(id="run-1", contract_id="contract-1"))
        plan = ProgramPlan(
            id="program-1",
            contract_id="contract-1",
            dag=PlanDAG((
                PlanStep(id="read-a", title="Read A", metadata={"read_only": True}),
                PlanStep(id="read-b", title="Read B", metadata={"read_only": True}),
            )),
        )

        pack = ContextPackBuilder(
            store,
            dag_execution_policy=DagExecutionPolicy(),
        ).build(
            run_id="run-1",
            program_plan=plan,
        )

        self.assertEqual(
            pack.metadata["dag_execution"]["parallel_candidate_step_ids"],
            ["read-a", "read-b"],
        )
        self.assertTrue(pack.metadata["dag_execution"]["can_parallelize"])

    def test_context_pack_rejects_a_different_plan_revision(self) -> None:
        store = InMemoryTaskStore()
        contract = self._contract()
        first_plan = self._program_plan()
        second_plan = ProgramPlan(
            id="program-2",
            contract_id=contract.id,
            dag=first_plan.dag,
        )
        store.save_contract(contract)
        store.save_plan(first_plan)
        store.save_plan(second_plan)
        store.create_run(
            TaskRun(
                id="run-1",
                contract_id=contract.id,
                plan_id=first_plan.id,
            )
        )

        with self.assertRaises(ValueError):
            ContextPackBuilder(store).build(
                run_id="run-1",
                program_plan=second_plan,
            )


if __name__ == "__main__":
    unittest.main()
