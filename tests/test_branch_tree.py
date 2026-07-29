from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.runtime import (  # noqa: E402
    BranchLineageError,
    RunBranchTreeBuilder,
    RunControlService,
)
from re_zlagent.harness.storage import InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    TaskContract,
    TaskRun,
)


class RunBranchTreeTests(unittest.TestCase):
    def _store(self) -> InMemoryTaskStore:
        store = InMemoryTaskStore()
        store.save_contract(
            TaskContract(
                id="contract",
                user_goal="branch safely",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        id="manual",
                        description="manual branch test",
                        type=CriterionType.HUMAN_APPROVAL,
                    ),
                ),
            )
        )
        store.create_run(TaskRun(id="root", contract_id="contract"))
        return store

    def test_projects_nested_forks_without_copying_history(self) -> None:
        store = self._store()
        controls = RunControlService(store)
        controls.fork("root", new_run_id="branch-a")
        controls.fork("root", new_run_id="branch-b")
        controls.fork("branch-a", new_run_id="branch-a-1")
        event_counts = {
            run.id: len(store.list_events(run.id))
            for run in store.list_runs()
        }

        tree = RunBranchTreeBuilder(store).build("branch-a-1")

        self.assertEqual(tree.root.run_id, "root")
        self.assertEqual(tree.node_count, 4)
        self.assertEqual(
            [child.run_id for child in tree.root.children],
            ["branch-a", "branch-b"],
        )
        self.assertTrue(tree.root.children[0].children[0].selected)
        self.assertEqual(
            event_counts,
            {
                run.id: len(store.list_events(run.id))
                for run in store.list_runs()
            },
        )

    def test_rejects_missing_parent_and_cycle(self) -> None:
        store = self._store()
        store.create_run(
            TaskRun(
                id="broken",
                contract_id="contract",
                metadata={"forked_from_run_id": "missing"},
            )
        )
        store.create_run(
            TaskRun(
                id="cycle-a",
                contract_id="contract",
                metadata={"forked_from_run_id": "cycle-b"},
            )
        )
        store.create_run(
            TaskRun(
                id="cycle-b",
                contract_id="contract",
                metadata={"forked_from_run_id": "cycle-a"},
            )
        )

        with self.assertRaises(BranchLineageError):
            RunBranchTreeBuilder(store).build("broken")
        with self.assertRaises(BranchLineageError):
            RunBranchTreeBuilder(store).build("cycle-a")


if __name__ == "__main__":
    unittest.main()
