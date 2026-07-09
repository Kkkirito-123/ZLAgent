from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.tools import RecommendedNextAction, ToolErrorType  # noqa: E402
from re_zlagent.harness.tools.builtins import ReadUrlTool, UrlFetchResult  # noqa: E402
from re_zlagent.harness.tools.builtins.url_tools import MAX_URL_BYTES  # noqa: E402


class FakeFetcher:
    def __init__(self, result: UrlFetchResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, int]] = []

    async def fetch(self, url: str, *, timeout_seconds: int) -> UrlFetchResult:
        self.calls.append((url, timeout_seconds))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ReadUrlToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_url_returns_freshness_evidence(self) -> None:
        fetched_at = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        fetcher = FakeFetcher(
            UrlFetchResult(
                url="https://example.com/page",
                status_code=200,
                body="hello",
                content_type="text/plain",
                fetched_at=fetched_at,
            )
        )
        tool = ReadUrlTool(fetcher)

        result = await tool.execute({"url": "https://example.com/page"})

        self.assertTrue(result.ok)
        self.assertIn("Fetched-At: 2026-07-09T12:00:00+00:00", result.content)
        self.assertEqual(result.evidence[0].type, "url")
        self.assertEqual(result.evidence[0].metadata["fetched_at"], fetched_at.isoformat())
        self.assertEqual(fetcher.calls, [("https://example.com/page", 10)])

    async def test_rejects_non_http_urls(self) -> None:
        tool = ReadUrlTool(FakeFetcher(RuntimeError("should not call")))

        result = await tool.execute({"url": "file:///etc/passwd"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, ToolErrorType.INVALID_INPUT)
        self.assertEqual(result.recommended_next_action, RecommendedNextAction.RETRY)

    async def test_truncates_large_body(self) -> None:
        tool = ReadUrlTool(
            FakeFetcher(
                UrlFetchResult(
                    url="https://example.com/large",
                    status_code=200,
                    body="a" * (MAX_URL_BYTES + 10),
                )
            )
        )

        result = await tool.execute({"url": "https://example.com/large"})

        self.assertTrue(result.ok)
        self.assertTrue(result.raw["truncated"])
        self.assertIn("[...truncated]", result.content)

    async def test_timeout_is_recoverable(self) -> None:
        tool = ReadUrlTool(FakeFetcher(TimeoutError("slow")))

        result = await tool.execute({"url": "https://example.com/slow"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, ToolErrorType.TIMEOUT)
        self.assertTrue(result.recoverable_by_model)

    async def test_external_failure_is_recoverable(self) -> None:
        tool = ReadUrlTool(FakeFetcher(RuntimeError("offline")))

        result = await tool.execute({"url": "https://example.com/offline"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, ToolErrorType.EXTERNAL_UNAVAILABLE)
        self.assertTrue(result.recoverable_by_model)


if __name__ == "__main__":
    unittest.main()
