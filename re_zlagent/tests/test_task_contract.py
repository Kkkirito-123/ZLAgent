from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    TaskContract,
    TaskRun,
    TaskRunStatus,
)


class TaskContractTests(unittest.TestCase):
    def _criterion(self) -> AcceptanceCriterion:
        return AcceptanceCriterion(
            id="tests-pass",
            description="unit tests pass",
            type=CriterionType.TEST_RESULT,
        )

    def test_contract_requires_goal_and_acceptance_criteria(self) -> None:
        with self.assertRaises(ValueError):
            TaskContract(id="task-1", user_goal="", acceptance_criteria=(self._criterion(),))

        with self.assertRaises(ValueError):
            TaskContract(id="task-1", user_goal="ship task", acceptance_criteria=())

    def test_contract_rejects_duplicate_acceptance_ids(self) -> None:
        criterion = self._criterion()

        with self.assertRaises(ValueError):
            TaskContract(
                id="task-1",
                user_goal="ship task",
                acceptance_criteria=(criterion, criterion),
            )

    def test_contract_normalizes_tuple_and_dict_fields(self) -> None:
        contract = TaskContract(
            id="task-1",
            user_goal="ship task",
            stakeholders=["owner"],
            mvp_scope=["core"],
            out_of_scope=["database"],
            acceptance_criteria=[self._criterion()],
            capability_boundaries={"storage": "adapter-later"},
            freshness_policy={"web": "explicit-evidence"},
        )

        self.assertEqual(contract.stakeholders, ("owner",))
        self.assertEqual(contract.mvp_scope, ("core",))
        self.assertEqual(contract.out_of_scope, ("database",))
        self.assertEqual(contract.capability_boundaries["storage"], "adapter-later")
        self.assertEqual(contract.required_criteria()[0].id, "tests-pass")

    def test_contract_is_immutable(self) -> None:
        contract = TaskContract(
            id="task-1",
            user_goal="ship task",
            acceptance_criteria=(self._criterion(),),
        )

        with self.assertRaises(FrozenInstanceError):
            contract.user_goal = "changed"  # type: ignore[misc]

    def test_task_run_with_status_preserves_context(self) -> None:
        run = TaskRun(
            id="run-1",
            contract_id="task-1",
            model_name="test-model",
            prompt_version="p1",
            metadata={"trace": "abc"},
        )

        updated = run.with_status(
            TaskRunStatus.RUNNING,
            checkpoint_id="chk-1",
            event_seq=2,
        )

        self.assertEqual(updated.status, TaskRunStatus.RUNNING)
        self.assertEqual(updated.current_checkpoint_id, "chk-1")
        self.assertEqual(updated.event_seq, 2)
        self.assertEqual(updated.model_name, "test-model")
        self.assertEqual(updated.metadata["trace"], "abc")


if __name__ == "__main__":
    unittest.main()
