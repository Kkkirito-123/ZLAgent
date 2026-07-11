from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.tasking import (  # noqa: E402
    DagExecutionPolicy,
    LongTaskProjector,
    PlanDAG,
    PlanStep,
    ProgramPlan,
)


class DagExecutionPolicyTests(unittest.TestCase):
    def test_parallel_candidates_are_conservative_frontier_steps(self) -> None:
        plan = ProgramPlan(
            id="program-1",
            contract_id="contract-1",
            dag=PlanDAG((
                PlanStep(
                    id="read-a",
                    title="Read A",
                    metadata={"read_only": True},
                ),
                PlanStep(
                    id="read-b",
                    title="Read B",
                    metadata={"read_only": True},
                ),
            )),
        )
        projection = LongTaskProjector().project(run_id="run-1", program_plan=plan)

        assessment = DagExecutionPolicy().assess(
            program_plan=plan,
            projection=projection,
        )

        self.assertTrue(assessment.can_parallelize)
        self.assertEqual(assessment.frontier_step_ids, ("read-a", "read-b"))
        self.assertEqual(assessment.parallel_candidate_step_ids, ("read-a", "read-b"))
        self.assertEqual(assessment.linear_only_step_ids, ())

    def test_confirm_tier_and_unknown_mutation_steps_stay_linear(self) -> None:
        plan = ProgramPlan(
            id="program-1",
            contract_id="contract-1",
            dag=PlanDAG((
                PlanStep(
                    id="send",
                    title="Send",
                    metadata={"allow_confirm": True},
                ),
                PlanStep(id="unknown", title="Unknown"),
            )),
        )
        projection = LongTaskProjector().project(run_id="run-1", program_plan=plan)

        assessment = DagExecutionPolicy().assess(
            program_plan=plan,
            projection=projection,
        )

        self.assertFalse(assessment.can_parallelize)
        self.assertEqual(assessment.linear_next_step_id, "send")
        self.assertEqual(assessment.parallel_candidate_step_ids, ())
        self.assertEqual(assessment.linear_only_step_ids, ("send", "unknown"))
        self.assertIn("confirm-tier step", assessment.unsafe_reasons["send"])
        self.assertIn(
            "not marked read-only or parallel-safe",
            assessment.unsafe_reasons["unknown"],
        )

    def test_write_target_conflicts_are_not_parallelized(self) -> None:
        plan = ProgramPlan(
            id="program-1",
            contract_id="contract-1",
            dag=PlanDAG((
                PlanStep(
                    id="write-a",
                    title="Write A",
                    metadata={
                        "parallel_safe": True,
                        "write_targets": ("file.txt",),
                    },
                ),
                PlanStep(
                    id="write-b",
                    title="Write B",
                    metadata={
                        "parallel_safe": True,
                        "write_targets": ("file.txt",),
                    },
                ),
            )),
        )
        projection = LongTaskProjector().project(run_id="run-1", program_plan=plan)

        assessment = DagExecutionPolicy().assess(
            program_plan=plan,
            projection=projection,
        )

        self.assertEqual(assessment.parallel_candidate_step_ids, ("write-a",))
        self.assertEqual(assessment.linear_only_step_ids, ("write-b",))
        self.assertIn("write target conflict: file.txt", assessment.unsafe_reasons["write-b"])


if __name__ == "__main__":
    unittest.main()
