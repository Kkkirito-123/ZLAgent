"""URL read tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

from ..base import Tool, ToolPermission, ToolResult
from ..metadata import Evidence, RecommendedNextAction, ToolErrorType

MAX_URL_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class UrlFetchResult:
    """Fetched URL payload."""

    url: str
    status_code: int
    body: str
    content_type: str = "text/plain"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))


class UrlFetcher(Protocol):
    """Boundary for URL fetching adapters."""

    async def fetch(self, url: str, *, timeout_seconds: int) -> UrlFetchResult:
        """Fetch a URL."""


class ReadUrlTool(Tool):
    """Read a URL via an injected fetcher."""

    name = "read_url"
    description = (
        "Read a URL and return text content with freshness evidence. Supports "
        "http and https URLs only."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        "required": ["url"],
    }

    def __init__(self, fetcher: UrlFetcher, *, max_bytes: int = MAX_URL_BYTES) -> None:
        self._fetcher = fetcher
        self._max_bytes = max(1, max_bytes)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        url = str(arguments.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ToolResult.failure(
                "url must be an absolute http(s) URL",
                error_type=ToolErrorType.INVALID_INPUT,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )
        timeout_seconds = int(arguments.get("timeout_seconds") or 10)
        timeout_seconds = max(1, min(timeout_seconds, 30))

        try:
            fetched = await self._fetcher.fetch(url, timeout_seconds=timeout_seconds)
        except TimeoutError as exc:
            return ToolResult.failure(
                f"read_url timed out: {exc}",
                error_type=ToolErrorType.TIMEOUT,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )
        except Exception as exc:  # noqa: BLE001 - fetch failures are data
            return ToolResult.failure(
                f"{type(exc).__name__}: {exc}",
                error_type=ToolErrorType.EXTERNAL_UNAVAILABLE,
                recoverable_by_model=True,
                recommended_next_action=RecommendedNextAction.RETRY,
                source=self.name,
            )

        encoded = fetched.body.encode("utf-8")
        truncated = len(encoded) > self._max_bytes
        body = encoded[: self._max_bytes].decode("utf-8", errors="replace")
        suffix = "\n\n[...truncated]" if truncated else ""
        header = (
            f"URL: {fetched.url}\n"
            f"Status: {fetched.status_code}\n"
            f"Content-Type: {fetched.content_type}\n"
            f"Fetched-At: {fetched.fetched_at.isoformat()}\n"
            f"Bytes: {len(encoded)}"
            f"{' (truncated)' if truncated else ''}\n\n"
        )
        return ToolResult.success(
            header + body + suffix,
            raw={
                "url": fetched.url,
                "status_code": fetched.status_code,
                "content_type": fetched.content_type,
                "bytes": len(encoded),
                "returned_bytes": len(body.encode("utf-8")),
                "truncated": truncated,
                "fetched_at": fetched.fetched_at.isoformat(),
            },
            evidence=[
                Evidence(
                    type="url",
                    ref=fetched.url,
                    summary="URL fetched",
                    metadata={
                        "status_code": fetched.status_code,
                        "content_type": fetched.content_type,
                        "bytes": len(encoded),
                        "truncated": truncated,
                        "fetched_at": fetched.fetched_at.isoformat(),
                    },
                )
            ],
            source=self.name,
        )
