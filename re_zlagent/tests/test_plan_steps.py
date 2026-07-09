from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.tasking import PlanDAG, PlanStep, StepStatus, StepVerifier  # noqa: E402


class PlanStepTests(unittest.TestCase):
    def test_step_requires_id_and_title(self) -> None:
        with self.assertRaises(ValueError):
            PlanStep(id="", title="read")
        with self.assertRaises(ValueError):
            PlanStep(id="step-1", title="")

    def test_verifier_passes_when_required_evidence_is_present(self) -> None:
        step = PlanStep(
            id="step-1",
            title="read file",
            required_evidence_refs=("file:read",),
        )

        result = StepVerifier().verify(
            step,
            tool_ok=True,
            evidence_refs=("file:read", "extra"),
            checkpoint_id="chk-1",
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.status, StepStatus.PASSED)
        self.assertEqual(result.checkpoint_id, "chk-1")

    def test_verifier_fails_when_required_evidence_is_missing(self) -> None:
        step = PlanStep(
            id="step-1",
            title="read file",
            required_evidence_refs=("file:read",),
        )

        result = StepVerifier().verify(
            step,
            tool_ok=True,
            evidence_refs=("other",),
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.status, StepStatus.FAILED)
        self.assertEqual(result.missing_evidence_refs, ("file:read",))

    def test_verifier_blocks_before_missing_evidence_checks(self) -> None:
        step = PlanStep(
            id="step-1",
            title="send message",
            required_evidence_refs=("message:sent",),
        )

        result = StepVerifier().verify(
            step,
            tool_ok=False,
            blocked=True,
            failure_reason="needs approval",
        )

        self.assertEqual(result.status, StepStatus.BLOCKED)
        self.assertEqual(result.reason, "needs approval")

    def test_plan_dag_returns_topological_order(self) -> None:
        dag = PlanDAG((
            PlanStep(id="read", title="read"),
            PlanStep(id="write", title="write", depends_on=("read",)),
            PlanStep(id="verify", title="verify", depends_on=("write",)),
        ))

        self.assertEqual(dag.topological_step_ids(), ("read", "write", "verify"))
        dag.validate_linear_order(("read", "write", "verify"))

    def test_plan_dag_rejects_missing_dependency(self) -> None:
        with self.assertRaises(ValueError):
            PlanDAG((
                PlanStep(id="write", title="write", depends_on=("read",)),
            ))

    def test_plan_dag_rejects_cycles(self) -> None:
        with self.assertRaises(ValueError):
            PlanDAG((
                PlanStep(id="a", title="a", depends_on=("b",)),
                PlanStep(id="b", title="b", depends_on=("a",)),
            ))

    def test_plan_dag_rejects_out_of_order_linear_execution(self) -> None:
        dag = PlanDAG((
            PlanStep(id="read", title="read"),
            PlanStep(id="write", title="write", depends_on=("read",)),
        ))

        with self.assertRaises(ValueError):
            dag.validate_linear_order(("write", "read"))


if __name__ == "__main__":
    unittest.main()
