from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.tasking import (  # noqa: E402
    Checkpoint,
    CheckpointStatus,
    InteractionKind,
    LongTaskProjector,
    PendingInteraction,
    PhaseStatus,
    PlanDAG,
    PlanStep,
    ProgramPhase,
    ProgramPlan,
    StepStatus,
    TaskEvent,
    TaskEventType,
)


class LongTaskProjectionTests(unittest.TestCase):
    def _plan(self) -> ProgramPlan:
        return ProgramPlan(
            id="program-1",
            contract_id="contract-1",
            dag=PlanDAG((
                PlanStep(id="collect", title="Collect context"),
                PlanStep(id="ask", title="Ask user", depends_on=("collect",)),
                PlanStep(id="verify", title="Verify", depends_on=("ask",)),
            )),
            phases=(
                ProgramPhase(
                    id="intake",
                    title="Intake",
                    step_ids=("collect",),
                ),
                ProgramPhase(
                    id="execution",
                    title="Execution",
                    step_ids=("ask", "verify"),
                ),
            ),
        )

    def _verified_event(self, seq: int, step_id: str, status: StepStatus) -> TaskEvent:
        return TaskEvent(
            id=f"evt-{seq}",
            run_id="run-1",
            seq=seq,
            type=TaskEventType.PLAN_STEP_VERIFIED,
            payload={
                "step_id": step_id,
                "status": status.value,
                "evidence_refs": [f"evidence:{step_id}"],
                "checkpoint_id": f"chk-{seq}",
            },
        )

    def test_frontier_advances_after_dependency_passes(self) -> None:
        projection = LongTaskProjector().project(
            run_id="run-1",
            program_plan=self._plan(),
            events=(self._verified_event(1, "collect", StepStatus.PASSED),),
        )

        self.assertEqual(projection.completed_step_ids, ("collect",))
        self.assertEqual(projection.frontier_step_ids, ("ask",))
        self.assertEqual(projection.step_runs["verify"].blocked_by, ("dependency:ask",))
        self.assertEqual(projection.phase_runs["intake"].status, PhaseStatus.PASSED)
        self.assertEqual(projection.phase_runs["execution"].status, PhaseStatus.PENDING)

    def test_pending_interaction_blocks_required_step_and_resume_token_is_kept(self) -> None:
        checkpoint = Checkpoint(
            id="chk-wait",
            run_id="run-1",
            seq=1,
            status=CheckpointStatus.WAITING_USER,
        )
        interaction = PendingInteraction(
            id="pi-1",
            run_id="run-1",
            checkpoint_id="chk-wait",
            question="Choose the next recovery strategy",
            required_by_step_id="ask",
            kind=InteractionKind.CLARIFICATION,
            resume_token="resume-token-1",
        )

        projection = LongTaskProjector().project(
            run_id="run-1",
            program_plan=self._plan(),
            events=(self._verified_event(1, "collect", StepStatus.PASSED),),
            checkpoints=(checkpoint,),
            pending_interactions=(interaction,),
        )

        self.assertEqual(projection.frontier_step_ids, ())
        self.assertEqual(projection.blocked_step_ids, ("ask",))
        self.assertEqual(projection.pending_interaction_ids, ("pi-1",))
        self.assertEqual(
            projection.step_runs["ask"].blocked_by,
            ("interaction:pi-1",),
        )
        self.assertEqual(projection.latest_checkpoint_id, "chk-wait")
        self.assertEqual(projection.latest_checkpoint_status, "waiting_user")

        resolved = interaction.resolve("Continue with alternative tool")
        self.assertFalse(resolved.open)
        self.assertEqual(resolved.resume_token, "resume-token-1")


if __name__ == "__main__":
    unittest.main()
