"""web_search: Tavily-first web search with HTML fallbacks.

v0.46 — the search stack now prefers **Tavily** for reliable semantic
search results, then falls back to HTML scraping providers when Tavily
is unavailable or misconfigured.

1. **Tavily** — primary source when ``TAVILY_API_KEY`` is configured.
2. **DuckDuckGo HTML** — fallback when Tavily is unavailable or returns
   no usable results.
3. **Bing HTML** — second fallback for light scraping resilience.
4. **SearXNG public instance** — last-resort meta-search; opt-in via
   ``ZLAGENT_SEARXNG_URL`` or ``SEARXNG_URL``.

Each provider is best-effort and the chain falls through on errors
instead of crashing the agent loop. The tool only returns ``ok=False``
when *every* configured provider has rejected the query — the error
string explains which provider failed and how.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from loguru import logger

from ..base import Tool, ToolPermission, ToolResult

TAVILY_ENDPOINT = "https://api.tavily.com/search"
DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
BING_ENDPOINT = "https://www.bing.com/search"
DEFAULT_TIMEOUT = 20.0
DEFAULT_RESULTS = 5
MAX_RESULTS_CAP = 10

_TAVILY_RESULT_LIMIT = MAX_RESULTS_CAP
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    " AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/124.0 Safari/537.36 ZLAgent/0.5"
)
_ACCEPT_LANG = "zh-CN,zh;q=0.9,en;q=0.8"

# DuckDuckGo result block. The snippet tag oscillates between <a> and
# <span>; we accept either.
_DDG_RESULT_BLOCK = re.compile(
    r'<a\s+rel="nofollow"\s+class="result__a"\s+href="(?P<href>[^"]+)"[^>]*>'
    r"(?P<title>.*?)</a>"
    r'.*?class="result__snippet"[^>]*>(?P<snippet>.*?)</(?:a|span)>',
    re.DOTALL | re.IGNORECASE,
)
# Bing result block: an <li class="b_algo"> wraps each hit.
# We pull the first <h2><a href=...>title</a> + the first <p>snippet</p>
# inside the block. Bing occasionally appends a class to the <p>; the
# greedy ``[^>]*`` swallows it.
_BING_RESULT_BLOCK = re.compile(
    r'<li\s+class="b_algo".*?<h2[^>]*>\s*<a\s+[^>]*href="(?P<href>[^"]+)"[^>]*>'
    r"(?P<title>.*?)</a>\s*</h2>"
    r".*?<p[^>]*>(?P<snippet>.*?)</p>",
    re.DOTALL | re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(raw: str) -> str:
    return _WS.sub(" ", _TAG.sub("", raw)).strip()


def _decode_ddg_href(href: str) -> str:
    target = href if href.startswith("http") else f"https:{href}"
    try:
        parsed = urlparse(target)
    except ValueError:
        return href
    params = parse_qs(parsed.query)
    uddg = params.get("uddg")
    if uddg:
        return unquote(uddg[0])
    return target


def _parse_ddg_html(html: str, *, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for match in _DDG_RESULT_BLOCK.finditer(html):
        results.append(
            {
                "title": _strip_html(match.group("title")),
                "url": _decode_ddg_href(match.group("href")),
                "snippet": _strip_html(match.group("snippet")),
            }
        )
        if len(results) >= limit:
            break
    return results


def _parse_bing_html(html: str, *, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for match in _BING_RESULT_BLOCK.finditer(html):
        href = match.group("href")
        # Bing occasionally embeds redirect URLs at
        # ``https://www.bing.com/ck/a?...&u=<encoded>``. Decode if so.
        if "/ck/a?" in href:
            try:
                parsed = urlparse(href)
                params = parse_qs(parsed.query)
                raw = params.get("u", [None])[0]
                if raw:
                    href = unquote(raw)
            except Exception:  # noqa: BLE001
                pass
        results.append(
            {
                "title": _strip_html(match.group("title")),
                "url": href,
                "snippet": _strip_html(match.group("snippet")),
            }
        )
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ProviderResult:
    """What a single provider attempt returned."""

    name: str
    results: list[dict[str, str]]
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.results)


async def _try_tavily(
    client: httpx.AsyncClient,
    query: str,
    *,
    top_k: int,
    api_key: str,
) -> _ProviderResult:
    if not api_key:
        return _ProviderResult("tavily", [], error="disabled (no api key)")
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": min(top_k, _TAVILY_RESULT_LIMIT),
        "search_depth": "advanced",
        "include_answer": False,
        "include_raw_content": False,
    }
    try:
        resp = await client.post(TAVILY_ENDPOINT, json=payload)
    except httpx.HTTPError as exc:
        return _ProviderResult("tavily", [], error=f"http: {exc}")
    if resp.status_code >= 400:
        return _ProviderResult("tavily", [], error=f"http {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        return _ProviderResult("tavily", [], error="non-json response")
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return _ProviderResult("tavily", [], error="empty results array")
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url_v = str(row.get("url") or row.get("raw_url") or "").strip()
        title = str(row.get("title") or "").strip()
        if not url_v or not title:
            continue
        snippet = str(row.get("content") or row.get("snippet") or "").strip()
        out.append({"title": title, "url": url_v, "snippet": snippet})
        if len(out) >= top_k:
            break
    if not out:
        return _ProviderResult("tavily", [], error="results array had no usable rows")
    return _ProviderResult("tavily", out)


async def _try_duckduckgo(
    client: httpx.AsyncClient, query: str, *, top_k: int,
) -> _ProviderResult:
    try:
        resp = await client.post(DDG_ENDPOINT, data={"q": query})
    except httpx.HTTPError as exc:
        return _ProviderResult("duckduckgo", [], error=f"http: {exc}")
    if resp.status_code >= 400:
        return _ProviderResult(
            "duckduckgo", [], error=f"http {resp.status_code}",
        )
    parsed = _parse_ddg_html(resp.text, limit=top_k)
    if not parsed:
        return _ProviderResult(
            "duckduckgo", [],
            error="empty result page (captcha / markup change)",
        )
    return _ProviderResult("duckduckgo", parsed)


async def _try_bing(
    client: httpx.AsyncClient, query: str, *, top_k: int,
) -> _ProviderResult:
    try:
        resp = await client.get(
            BING_ENDPOINT,
            params={"q": query, "setlang": "zh-cn"},
        )
    except httpx.HTTPError as exc:
        return _ProviderResult("bing", [], error=f"http: {exc}")
    if resp.status_code >= 400:
        return _ProviderResult("bing", [], error=f"http {resp.status_code}")
    parsed = _parse_bing_html(resp.text, limit=top_k)
    if not parsed:
        return _ProviderResult(
            "bing", [],
            error="empty result page (captcha / markup change)",
        )
    return _ProviderResult("bing", parsed)


async def _try_searxng(
    client: httpx.AsyncClient,
    query: str,
    *,
    top_k: int,
    base_url: str,
) -> _ProviderResult:
    """Hit a SearXNG instance's JSON ``/search?format=json`` surface.

    SearXNG is a self-hostable meta-search engine; a single instance
    URL is enough. Some public instances disable the JSON output for
    abuse-prevention, in which case we degrade silently to the HTML
    parsers above.
    """
    if not base_url:
        return _ProviderResult("searxng", [], error="disabled (no url)")
    url = base_url.rstrip("/") + "/search"
    try:
        resp = await client.get(
            url,
            params={"q": query, "format": "json", "safesearch": "1"},
        )
    except httpx.HTTPError as exc:
        return _ProviderResult("searxng", [], error=f"http: {exc}")
    if resp.status_code >= 400:
        return _ProviderResult(
            "searxng", [], error=f"http {resp.status_code}",
        )
    try:
        payload = resp.json()
    except ValueError:
        return _ProviderResult("searxng", [], error="non-json response")
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return _ProviderResult(
            "searxng", [], error="empty results array",
        )
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url_v = str(row.get("url") or "").strip()
        title = str(row.get("title") or "").strip()
        if not url_v or not title:
            continue
        snippet = str(row.get("content") or row.get("snippet") or "").strip()
        out.append({"title": title, "url": url_v, "snippet": snippet})
        if len(out) >= top_k:
            break
    if not out:
        return _ProviderResult(
            "searxng", [], error="results array had no usable rows",
        )
    return _ProviderResult("searxng", out)


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the public web and return the top results as (title, url,"
        " snippet) triples. Use this to discover sources when the user asks"
        " about current events, unfamiliar facts, or to find a URL to then"
        " feed into read_url. When the user says current/latest/this year/"
        "今年/最新, include the current year from the system prompt in the"
        " query and verify source dates instead of assuming an older year.\n"
        "\n"
        "v0.46+: the tool drives a provider chain Tavily → DuckDuckGo →"
        " Bing → optional SearXNG. One provider failing is no longer a"
        " fatal error — the fallback chain kicks in transparently."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    is_destructive = False
    max_result_chars = 4_000
    search_hint = "search web duckduckgo current information discover urls"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query string.",
            },
            "top_k": {
                "type": "integer",
                "description": (
                    f"Number of results to return (1-{MAX_RESULTS_CAP}, default {DEFAULT_RESULTS})."
                ),
                "minimum": 1,
                "maximum": MAX_RESULTS_CAP,
            },
        },
        "required": ["query"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, content="", error="query is required")
        try:
            top_k = int(arguments.get("top_k") or DEFAULT_RESULTS)
        except (TypeError, ValueError):
            top_k = DEFAULT_RESULTS
        top_k = max(1, min(top_k, MAX_RESULTS_CAP))

        tavily_key = (
            os.environ.get("TAVILY_API_KEY")
            or os.environ.get("TAVILY_KEY")
            or ""
        ).strip()
        searxng_url = (
            os.environ.get("ZLAGENT_SEARXNG_URL")
            or os.environ.get("SEARXNG_URL")
            or ""
        ).strip()

        attempts: list[_ProviderResult] = []
        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            trust_env=True,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": _ACCEPT_LANG},
        ) as client:
            for provider in self._provider_order(tavily_key, searxng_url):
                if provider == "tavily":
                    result = await _try_tavily(
                        client, query, top_k=top_k, api_key=tavily_key,
                    )
                elif provider == "duckduckgo":
                    result = await _try_duckduckgo(client, query, top_k=top_k)
                elif provider == "bing":
                    result = await _try_bing(client, query, top_k=top_k)
                elif provider == "searxng":
                    result = await _try_searxng(
                        client, query, top_k=top_k, base_url=searxng_url,
                    )
                else:
                    continue
                attempts.append(result)
                if result.ok:
                    return _render_results(
                        query, result.results, provider_name=result.name,
                        attempts=attempts,
                    )
                logger.info(
                    "[web_search] provider '{}' failed: {}; trying next",
                    result.name, result.error,
                )

        failure_summary = "; ".join(f"{a.name}={a.error or 'empty'}" for a in attempts)
        return ToolResult(
            ok=False, content="",
            error=(
                f"all search providers failed ({failure_summary})."
                " If this keeps happening, set ZLAGENT_SEARXNG_URL to"
                " a known-good SearXNG instance for a third fallback."
            ),
        )

    def _provider_order(self, tavily_key: str, searxng_url: str) -> list[str]:
        """Decide which providers to try, in priority order.

        Tavily is preferred when configured because it returns semantic
        search results and reduces scrape-breakage risk. HTML fallbacks
        remain available for resilience.
        """
        order = ["tavily", "duckduckgo", "bing"]
        if searxng_url:
            order.append("searxng")
        return order


def _render_results(
    query: str,
    results: list[dict[str, str]],
    *,
    provider_name: str,
    attempts: list[_ProviderResult],
) -> ToolResult:
    lines = [f"Search results for: {query}", f"(via {provider_name})", ""]
    # If the primary failed and a later provider succeeded, leave a
    # one-line breadcrumb so the LLM can see which sources were tried
    # (useful for diagnosing "why didn't I get the usual DDG hit?").
    fallback_msgs = [
        f"{a.name}={a.error}" for a in attempts[:-1] if a.error
    ]
    if fallback_msgs:
        lines.append(f"(fallback chain: {'; '.join(fallback_msgs)})")
        lines.append("")
    for idx, item in enumerate(results, 1):
        lines.append(f"{idx}. {item['title']}")
        lines.append(f"   URL: {item['url']}")
        snippet = item["snippet"]
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")
    return ToolResult(ok=True, content="\n".join(lines).strip())
