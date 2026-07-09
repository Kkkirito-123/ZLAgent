from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from harness.tools import ReadBeforeWritePolicy, WritePreconditionStatus  # noqa: E402


class ReadBeforeWritePolicyTests(unittest.TestCase):
    def test_creating_new_target_is_allowed(self) -> None:
        policy = ReadBeforeWritePolicy()

        result = policy.check_write(
            target="/tmp/new.txt",
            current_hash=None,
            creating_new_target=True,
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.status, WritePreconditionStatus.ALLOW)

    def test_existing_target_requires_current_hash(self) -> None:
        policy = ReadBeforeWritePolicy()

        result = policy.check_write(target="/tmp/a.txt", current_hash=None)

        self.assertFalse(result.allowed)
        self.assertEqual(result.required_action, "read_before_write")
        self.assertIn("current target hash", result.reason)

    def test_existing_target_requires_prior_read_mark(self) -> None:
        policy = ReadBeforeWritePolicy()

        result = policy.check_write(target="/tmp/a.txt", current_hash="hash-a")

        self.assertFalse(result.allowed)
        self.assertEqual(result.required_action, "read_before_write")
        self.assertIn("has not been read", result.reason)

    def test_matching_read_mark_allows_write(self) -> None:
        policy = ReadBeforeWritePolicy()
        mark = policy.stamp_read(
            target="/tmp/a.txt",
            content_hash="hash-a",
            metadata={"tool_call_id": "call-1"},
        )

        result = policy.check_write(target="/tmp/a.txt", current_hash="hash-a")

        self.assertTrue(result.allowed)
        self.assertEqual(policy.latest_mark("/tmp/a.txt"), mark)
        self.assertEqual(mark.metadata["tool_call_id"], "call-1")

    def test_changed_target_hash_denies_write(self) -> None:
        policy = ReadBeforeWritePolicy()
        policy.stamp_read(target="/tmp/a.txt", content_hash="hash-a")

        result = policy.check_write(target="/tmp/a.txt", current_hash="hash-b")

        self.assertFalse(result.allowed)
        self.assertEqual(result.required_action, "read_before_write")
        self.assertIn("changed since last read", result.reason)


if __name__ == "__main__":
    unittest.main()
