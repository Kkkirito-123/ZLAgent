"""read_url: fetch an HTTP(S) URL and return its readable text content.

Design notes
------------
* ``httpx`` is already a project dependency (used by the LLM client); reusing
  it keeps the deploy surface small.
* HTML is extracted with the stdlib ``html.parser`` — no new dependency and
  good enough for "feed this page into an LLM" use cases. Full DOM fidelity
  is not a goal; the parser strips ``<script>``/``<style>``/``<noscript>``
  and collapses whitespace.
* Hard caps keep a single tool call from blowing up the LLM context window or
  the agent's memory: max 2 MiB wire bytes, max 5 k chars returned text.
* A coarse SSRF guard blocks obvious localhost / loopback hosts. This is not
  a replacement for a real network policy — it is the minimum the tool owes
  before shipping.
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

import httpx

from ..base import Tool, ToolPermission, ToolResult

MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 5_000
DEFAULT_TIMEOUT = 20.0
SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}
BLOCK_HOST_SUBSTRINGS = ("localhost", "127.", "0.0.0.0", "::1", "169.254.")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    def result(self) -> str:
        return "\n".join(self._parts)


def _extract_text(body: str, content_type: str) -> str:
    ctype = content_type.lower()
    looks_like_html = "html" in ctype or "<html" in body[:2000].lower()
    if not looks_like_html:
        return body
    parser = _TextExtractor()
    try:
        parser.feed(body)
    except Exception:  # noqa: BLE001 - defensive; fall back to raw body
        return body
    return parser.result() or body


def _is_blocked_host(url: str) -> bool:
    lowered = url.lower()
    return any(sub in lowered for sub in BLOCK_HOST_SUBSTRINGS)


class ReadUrlTool(Tool):
    name = "read_url"
    description = (
        "Fetch a public HTTP(S) URL and return its readable plain-text content."
        " Strips HTML/script/style and returns up to 5,000 characters (v0.40.7:"
        " tightened from 12k to keep prompt growth under control when the LLM"
        " reads multiple pages in one turn). If you truly need the full body,"
        " fetch the URL twice with different focused queries instead of asking"
        " for more characters."
        " Use this when the user gives you a link or whenever you need to"
        " ground your answer in the contents of a specific page."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    is_destructive = False
    max_result_chars = 5_000
    search_hint = "fetch url webpage html article content public http https"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute URL to fetch. Must be http:// or https://.",
            }
        },
        "required": ["url"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        url = str(arguments.get("url") or "").strip()
        if not url:
            return ToolResult(ok=False, content="", error="url is required")
        scheme_sep = url.find("://")
        scheme = url[:scheme_sep].lower() if scheme_sep >= 0 else ""
        if scheme not in {"http", "https"}:
            return ToolResult(
                ok=False, content="", error=f"unsupported scheme: {scheme or '(empty)'}"
            )
        if _is_blocked_host(url):
            return ToolResult(ok=False, content="", error="host blocked by SSRF guard")

        try:
            async with httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True,
                trust_env=True,
                headers={"User-Agent": "ZLAgent/0.5 (+read_url tool)"},
            ) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, content="", error=f"fetch failed: {exc}")

        if resp.status_code >= 400:
            return ToolResult(
                ok=False,
                content="",
                error=f"HTTP {resp.status_code} fetching {url}",
            )

        content_type = resp.headers.get("content-type", "")
        raw = resp.content[:MAX_CONTENT_BYTES]
        try:
            body = raw.decode(resp.encoding or "utf-8", errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")

        text = _extract_text(body, content_type)
        truncated = False
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS]
            truncated = True

        header = (
            f"URL: {resp.url}\n"
            f"Status: {resp.status_code}\n"
            f"Content-Type: {content_type or 'unknown'}\n"
        )
        if truncated:
            header += f"Note: output truncated to {MAX_OUTPUT_CHARS:,} chars.\n"
        return ToolResult(ok=True, content=f"{header}\n{text}")
