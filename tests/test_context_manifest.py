from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.context import (  # noqa: E402
    ContextInput,
    ContextManifestBuilder,
    ContextTrust,
)


class ContextManifestTests(unittest.TestCase):
    def test_budget_priority_truncation_and_safe_metadata(self) -> None:
        manifest = ContextManifestBuilder(max_chars=256).build(
            (
                ContextInput(
                    id="host",
                    source="request",
                    trust=ContextTrust.UNTRUSTED,
                    content="a" * 220,
                    max_chars=180,
                ),
                ContextInput(
                    id="memory",
                    source="memory",
                    trust=ContextTrust.RECALLED_BACKGROUND,
                    content="private preference " * 20,
                ),
            )
        )

        self.assertEqual(manifest.used_chars, 256)
        self.assertTrue(manifest.truncated)
        self.assertEqual(manifest.segments[0].included_chars, 180)
        self.assertEqual(manifest.segments[1].included_chars, 76)
        data = manifest.to_dict()
        self.assertNotIn("content", data["segments"][0])
        self.assertNotIn("private preference", str(data))
        self.assertIn('trust="recalled_background"', manifest.render())

    def test_duplicate_segment_ids_are_rejected(self) -> None:
        item = ContextInput(
            id="same",
            source="test",
            trust=ContextTrust.HOST,
            content="content",
        )

        with self.assertRaises(ValueError):
            ContextManifestBuilder(max_chars=256).build((item, item))


if __name__ == "__main__":
    unittest.main()
