from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from ..base import Tool, ToolPermission, ToolResult

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ...graph.llm_extractor import LLMGraphExtractor

_MODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SLUG_KEEP_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 20_000
MAX_TAGS = 20


class KnowledgeIngestTool(Tool):
    name = "knowledge_ingest"
    description = (
        "Write a confirmed knowledge entry into an existing domain knowledge-mode"
        " wiki under `workspace/knowledge_modes/<mode_id>/wiki/outputs/`. Use this"
        " only after the user agrees to save a reviewed answer/source into a"
        " knowledge base. Before calling it, inspect existing modes with"
        " `knowledge_inspect`; if no suitable mode exists, ask the user whether to"
        " create one with `knowledge_mode_manage`. This tool updates the output page,"
        " wiki/index.md, and wiki/log.md atomically per call.\n\n"
        "HARD RULE — one ingest = one entry. If the user asks to save N papers /"
        " articles / items, call this tool N separate times (one title +"
        " summary_markdown per call). DO NOT pack multiple papers into a single"
        " summary_markdown — that produces a single output page where only the"
        " first item gets full detail and the rest become tag stubs."
    )
    # Personal-AI mode: knowledge writes are to the operator's own
    # workspace; per-call yes/no would make ingesting a list of papers
    # painful. Use ``knowledge_inspect`` (safe, read-only) to verify.
    permission = ToolPermission.SAFE
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = False
    max_result_chars = 8_000
    search_hint = "knowledge ingest save wiki output paper source confirmed entry"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["ingest"],
                "description": "Must be 'ingest'.",
            },
            "mode_id": {
                "type": "string",
                "description": "Existing knowledge mode id, e.g. ai-paper, travel, study.",
            },
            "title": {
                "type": "string",
                "description": "Human-readable entry title.",
            },
            "summary_markdown": {
                "type": "string",
                "description": "Structured Markdown body to save.",
            },
            "source_url": {
                "type": "string",
                "description": "Optional source URL for provenance and duplicate checks.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional topic/domain tags.",
            },
        },
        "required": ["action", "mode_id", "title", "summary_markdown"],
    }

    def __init__(
        self,
        workspace_dir: Path,
        *,
        graph_extractor: Optional["LLMGraphExtractor"] = None,
    ) -> None:
        self._workspace = Path(workspace_dir).resolve()
        self._root = (self._workspace / "knowledge_modes").resolve()
        self._graph_extractor = graph_extractor

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        if action != "ingest":
            return ToolResult(ok=False, content="", error="action must be 'ingest'")
        mode_dir = self._mode_dir(arguments)
        if isinstance(mode_dir, ToolResult):
            return mode_dir
        title = _clean_single_line(arguments.get("title"), field="title", max_chars=MAX_TITLE_CHARS)
        if isinstance(title, ToolResult):
            return title
        summary = str(arguments.get("summary_markdown") or "").strip()
        if not summary:
            return ToolResult(ok=False, content="", error="summary_markdown is required")
        if len(summary) > MAX_SUMMARY_CHARS:
            return ToolResult(ok=False, content="", error=f"summary_markdown exceeds {MAX_SUMMARY_CHARS} chars")
        source_url = _clean_single_line(arguments.get("source_url"), field="source_url", max_chars=2_000, required=False)
        if isinstance(source_url, ToolResult):
            return source_url
        tags = _parse_tags(arguments.get("tags"))

        outputs_dir = mode_dir / "wiki" / "outputs"
        index_path = mode_dir / "wiki" / "index.md"
        log_path = mode_dir / "wiki" / "log.md"
        if not outputs_dir.is_dir():
            return ToolResult(ok=False, content="", error=f"wiki/outputs not found for mode {mode_dir.name!r}")
        if not index_path.is_file():
            return ToolResult(ok=False, content="", error=f"wiki/index.md not found for mode {mode_dir.name!r}")
        if not log_path.is_file():
            return ToolResult(ok=False, content="", error=f"wiki/log.md not found for mode {mode_dir.name!r}")

        duplicate = self._find_duplicate(outputs_dir, title, source_url)
        if duplicate is not None:
            return ToolResult(ok=False, content="", error=f"entry already exists: {duplicate.name}")

        filename = _entry_filename(title, source_url)
        target = (outputs_dir / filename).resolve()
        try:
            target.relative_to(outputs_dir.resolve())
        except ValueError:
            return ToolResult(ok=False, content="", error="output path escapes wiki/outputs")
        if target.exists():
            return ToolResult(ok=False, content="", error=f"entry already exists: {filename}")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        content = _render_entry(
            title=title,
            mode_id=mode_dir.name,
            source_url=source_url,
            tags=tags,
            ingested_at=now,
            summary=summary,
        )
        try:
            target.write_text(content, encoding="utf-8")
            with index_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n- [[outputs/{filename}|{title}]]")
            with log_path.open("a", encoding="utf-8") as fh:
                source_text = f" source={source_url}" if source_url else ""
                fh.write(f"\n## [{now}] ingest | {target.stem}\n\nSaved `{title}` to `wiki/outputs/{filename}`.{source_text}\n")
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"ingest failed: {exc}")

        rel_path = f"knowledge_modes/{mode_dir.name}/wiki/outputs/{filename}"

        # v1.2 — fire-and-forget LLM graph extraction so the next /api/knowledge-bases
        # call sees a real Paper/Keyword/Author graph instead of the heuristic
        # "Key/Points/Smoke" placeholders. Failures are logged and never affect
        # the ingest result the operator just confirmed.
        self._schedule_graph_preheat(
            mode_id=mode_dir.name,
            title=title,
            summary=summary,
            source_url=source_url,
            tags=tags,
            ingested_at=now,
            stem=target.stem,
            filename=filename,
        )

        return ToolResult(
            ok=True,
            content=f"Knowledge entry saved to {rel_path}",
            raw={"mode_id": mode_dir.name, "path": rel_path, "title": title, "source_url": source_url, "tags": tags},
        )

    def _schedule_graph_preheat(
        self,
        *,
        mode_id: str,
        title: str,
        summary: str,
        source_url: str,
        tags: list[str],
        ingested_at: str,
        stem: str,
        filename: str,
    ) -> None:
        if self._graph_extractor is None or not self._graph_extractor.configured:
            return
        try:
            from ...graph.source import GraphSourceRecord, _split_chunks  # local import keeps tool light
        except Exception:  # noqa: BLE001 — defensive
            return
        record = GraphSourceRecord(
            id=f"mode:{mode_id}:{stem}",
            source_type="paper",
            knowledge_base_id=mode_id,
            title=title,
            summary=summary[:200] + ("…" if len(summary) > 200 else ""),
            content=summary,
            tags=list(tags),
            attrs={
                "source_url": source_url,
                "path": f"knowledge_modes/{mode_id}/wiki/outputs/{filename}",
                "kind": "wiki_output",
            },
            chunks=_split_chunks(summary),
            created_at=ingested_at,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop — caller is sync; skip preheat (rebuild-graph endpoint covers it)
        extractor = self._graph_extractor

        async def _runner() -> None:
            try:
                await extractor.extract_and_cache(record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ingest_preheat] {} failed: {}", record.id, exc)

        loop.create_task(_runner())

    def _mode_dir(self, arguments: dict[str, Any]) -> Path | ToolResult:
        mode_id = str(arguments.get("mode_id") or "").strip()
        if not mode_id:
            return ToolResult(ok=False, content="", error="mode_id is required")
        if not _MODE_ID_RE.match(mode_id):
            return ToolResult(ok=False, content="", error="mode_id must match [a-z0-9][a-z0-9._-]{0,63}")
        mode_dir = (self._root / mode_id).resolve()
        try:
            mode_dir.relative_to(self._root)
        except ValueError:
            return ToolResult(ok=False, content="", error="mode path escapes knowledge_modes root")
        if not mode_dir.is_dir() or not (mode_dir / "MODE.md").is_file():
            return ToolResult(ok=False, content="", error=f"knowledge mode {mode_id!r} not found; inspect/create it first")
        return mode_dir

    @staticmethod
    def _find_duplicate(outputs_dir: Path, title: str, source_url: str) -> Path | None:
        title_line = f"# {title}"
        for path in outputs_dir.glob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if source_url and f"- source_url: {source_url}" in text:
                return path
            if title_line in text.splitlines()[:3]:
                return path
        return None


def _clean_single_line(value: Any, *, field: str, max_chars: int, required: bool = True) -> str | ToolResult:
    text = str(value or "").strip()
    if not text:
        if required:
            return ToolResult(ok=False, content="", error=f"{field} is required")
        return ""
    if "\n" in text or "\r" in text:
        return ToolResult(ok=False, content="", error=f"{field} must be a single line")
    if len(text) > max_chars:
        return ToolResult(ok=False, content="", error=f"{field} exceeds {max_chars} chars")
    return text


def _parse_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        raw = re.split(r"[,，\n]", value)
    else:
        raw = []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = str(item or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:50])
        if len(out) >= MAX_TAGS:
            break
    return out


def _entry_filename(title: str, source_url: str) -> str:
    seed = source_url or title
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    slug = _SLUG_KEEP_RE.sub("-", title.lower()).strip("-")[:80].strip("-")
    if not slug:
        slug = "entry"
    return f"{slug}-{digest}.md"


def _render_entry(*, title: str, mode_id: str, source_url: str, tags: list[str], ingested_at: str, summary: str) -> str:
    tag_text = ", ".join(tags)
    source_text = source_url or ""
    return (
        f"# {title}\n\n"
        f"- mode_id: {mode_id}\n"
        f"- source_url: {source_text}\n"
        f"- tags: {tag_text}\n"
        f"- ingested_at: {ingested_at}\n\n"
        "## Summary\n\n"
        f"{summary.strip()}\n"
    )
