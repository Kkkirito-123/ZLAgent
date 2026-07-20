from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from backend.skills.daily_review_service import DailyReviewService
from backend.core.config import Settings
from backend.tools.builtins.read_file import ReadFileTool


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_url_accepts_compose_environment_name(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql+psycopg://user:pass@postgres/db"},
            clear=False,
        ):
            settings = Settings(_env_file=None)

        self.assertEqual(
            settings.database_url,
            "postgresql+psycopg://user:pass@postgres/db",
        )

    async def test_read_file_can_be_constructed_without_usage_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "note.md").write_text("hello", encoding="utf-8")
            result = await ReadFileTool(root).execute({"path": "note.md"})

        self.assertTrue(result.ok)
        self.assertIn("hello", result.content)

    async def test_daily_review_user_block_accepts_graph_review(self) -> None:
        service = DailyReviewService.__new__(DailyReviewService)
        service._lookback = 86_400
        block = service._build_user_block(
            {},
            graph_review={
                "status": "queued",
                "reason": "test",
                "candidates": [],
                "graph_items": [],
            },
        )

        self.assertIn("GraphRAG 夜间候选", block)


if __name__ == "__main__":
    unittest.main()
