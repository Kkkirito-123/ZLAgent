from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    AcceptanceGate,
    AcceptanceStatus,
    CriterionStatus,
    CriterionType,
    TaskContract,
)


class AcceptanceGateTests(unittest.TestCase):
    def _contract(self, *criteria: AcceptanceCriterion) -> TaskContract:
        return TaskContract(
            id="task-1",
            user_goal="ship task",
            acceptance_criteria=criteria,
        )

    def test_missing_required_evidence_fails_completion(self) -> None:
        contract = self._contract(
            AcceptanceCriterion(
                id="file-evidence",
                description="file was written",
                type=CriterionType.TOOL_EVIDENCE,
                evidence_refs=("file:output.md",),
            )
        )

        decision = AcceptanceGate().evaluate(contract, evidence_refs=())

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.status, AcceptanceStatus.FAILED)
        self.assertEqual(decision.criteria["file-evidence"], CriterionStatus.FAILED)

    def test_tool_evidence_and_test_results_pass(self) -> None:
        contract = self._contract(
            AcceptanceCriterion(
                id="file-evidence",
                description="file was written",
                type=CriterionType.TOOL_EVIDENCE,
                evidence_refs=("file:output.md",),
            ),
            AcceptanceCriterion(
                id="unit-tests",
                description="unit tests pass",
                type=CriterionType.TEST_RESULT,
            ),
        )

        decision = AcceptanceGate().evaluate(
            contract,
            evidence_refs={"file:output.md"},
            passed_tests={"unit-tests"},
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.status, AcceptanceStatus.PASSED)
        self.assertEqual(
            set(decision.passed_criteria),
            {"file-evidence", "unit-tests"},
        )

    def test_human_approval_blocks_until_approval_arrives(self) -> None:
        contract = self._contract(
            AcceptanceCriterion(
                id="approval",
                description="user approved mutation",
                type=CriterionType.HUMAN_APPROVAL,
            )
        )
        gate = AcceptanceGate()

        blocked = gate.evaluate(contract)
        approved = gate.evaluate(contract, human_approvals={"approval"})

        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.status, AcceptanceStatus.BLOCKED)
        self.assertEqual(blocked.blocked_criteria, ("approval",))
        self.assertTrue(approved.accepted)

    def test_freshness_requires_recent_verified_at(self) -> None:
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        contract = self._contract(
            AcceptanceCriterion(
                id="fresh-news",
                description="fresh source checked",
                type=CriterionType.FRESHNESS,
                evidence_refs=("web:source",),
                freshness_window_seconds=60,
            )
        )
        gate = AcceptanceGate()

        missing = gate.evaluate(contract, now=now)
        stale = gate.evaluate(
            contract,
            freshness_by_ref={"web:source": now - timedelta(seconds=90)},
            now=now,
        )
        fresh = gate.evaluate(
            contract,
            freshness_by_ref={"web:source": now - timedelta(seconds=30)},
            now=now,
        )

        self.assertEqual(missing.status, AcceptanceStatus.BLOCKED)
        self.assertEqual(stale.status, AcceptanceStatus.FAILED)
        self.assertTrue(fresh.accepted)

    def test_optional_failure_does_not_block_completion(self) -> None:
        contract = self._contract(
            AcceptanceCriterion(
                id="unit-tests",
                description="unit tests pass",
                type=CriterionType.TEST_RESULT,
            ),
            AcceptanceCriterion(
                id="nice-extra",
                description="optional extra evidence",
                type=CriterionType.TOOL_EVIDENCE,
                required=False,
                evidence_refs=("optional:evidence",),
            ),
        )

        decision = AcceptanceGate().evaluate(contract, passed_tests={"unit-tests"})

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.status, AcceptanceStatus.PASSED)
        self.assertEqual(decision.optional_failed_criteria, ("nice-extra",))


if __name__ == "__main__":
    unittest.main()
