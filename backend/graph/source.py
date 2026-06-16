from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ..db.models import UserMemory
from ..db.session import session_scope


@dataclass(slots=True)
class GraphSourceRecord:
    id: str
    source_type: str
    knowledge_base_id: str
    title: str
    summary: str
    content: str
    tags: list[str]
    attrs: dict[str, Any]
    chunks: list[str]
    created_at: str | None = None


@dataclass(slots=True)
class GraphSourceBundle:
    records: list[GraphSourceRecord]
    summary: dict[str, Any]
    pending_review: list[GraphSourceRecord] = None


def _record_from_memory(mem: UserMemory) -> GraphSourceRecord:
    content = (mem.content or "").strip()
    summary = content[:140] + ("…" if len(content) > 140 else "")
    chunks = _split_chunks(content)
    return GraphSourceRecord(
        id=f"memory:{mem.id}",
        source_type="memory",
        knowledge_base_id=mem.knowledge_base_id or "default",
        title=f"记忆 #{mem.id}",
        summary=summary,
        content=content,
        tags=[mem.kind, mem.source, mem.knowledge_base_id],
        attrs={
            "kind": mem.kind,
            "source": mem.source,
            "pinned": bool(mem.pinned),
            "archived": bool(mem.archived),
            "recall_count": int(mem.recall_count or 0),
        },
        chunks=chunks,
        created_at=mem.created_at.isoformat() if mem.created_at else None,
    )


def collect_graph_records(
    knowledge_base_id: str = "default",
    *,
    workspace_dir: Path | None = None,
) -> GraphSourceBundle:
    records: list[GraphSourceRecord] = []
    pending_review: list[GraphSourceRecord] = []
    summary = {"memory": 0, "paper": 0, "task": 0, "runtime": 0, "manual": 0}
    with session_scope() as session:
        memories = (
            session.query(UserMemory)
            .filter(UserMemory.knowledge_base_id == knowledge_base_id)
            .order_by(UserMemory.created_at.desc())
            .limit(100)
            .all()
        )
        for mem in memories:
            records.append(_record_from_memory(mem))
            summary["memory"] += 1

    if workspace_dir is not None and knowledge_base_id != "default":
        for record in iter_knowledge_mode_records(workspace_dir, knowledge_base_id):
            records.append(record)
            summary["paper"] += 1

    if knowledge_base_id == "default":
        records.extend(
            [
                GraphSourceRecord(
                    id="runtime:turn-context",
                    source_type="runtime",
                    knowledge_base_id="default",
                    title="TurnContext / Runtime",
                    summary="Agent turn、runtime 状态、工具调用和消息路由。",
                    content="TurnContext, RuntimeSnapshot, tool registry, gateway manager",
                    tags=["runtime", "turn_context", "agent"],
                    attrs={"priority": "high", "kind": "context"},
                    chunks=_split_chunks("TurnContext, RuntimeSnapshot, tool registry, gateway manager"),
                ),
                GraphSourceRecord(
                    id="manual:graph-policy",
                    source_type="manual",
                    knowledge_base_id="default",
                    title="图谱把关策略",
                    summary="先抽取，再让 LLM 审核，最后生成思维导图。",
                    content="LLM should review extracted items before they become graph nodes.",
                    tags=["policy", "llm_gate", "mindmap"],
                    attrs={"priority": "high", "kind": "policy"},
                    chunks=_split_chunks("LLM should review extracted items before they become graph nodes."),
                ),
            ]
        )
        summary["runtime"] += 1
        summary["manual"] += 1

    if knowledge_base_id != "default":
        pending_review.append(
            GraphSourceRecord(
                id=f"manual:{knowledge_base_id}:overview",
                source_type="manual",
                knowledge_base_id=knowledge_base_id,
                title=f"{knowledge_base_id} 子图概览",
                summary="该知识库会先做结构化抽取，再进入 LLM 审核层。",
                content="This knowledge base uses extraction + llm gate + mindmap generation.",
                tags=["overview", "mindmap", knowledge_base_id],
                attrs={"kind": "overview"},
                chunks=_split_chunks("This knowledge base uses extraction + llm gate + mindmap generation."),
            )
        )
        summary["manual"] += 1

    return GraphSourceBundle(records=records, summary=summary, pending_review=pending_review)


def _split_chunks(text: str, *, max_chars: int = 160) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for part in re.split(r"(?<=[。！？!?\.])\s+|\n+", text):
        piece = part.strip()
        if not piece:
            continue
        if current_len + len(piece) > max_chars and current:
            chunks.append(" ".join(current).strip())
            current = [piece]
            current_len = len(piece)
        else:
            current.append(piece)
            current_len += len(piece) + 1
    if current:
        chunks.append(" ".join(current).strip())
    return chunks or [text[:max_chars]]


_FRONTMATTER_RE = re.compile(r"^- ([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$")


def list_knowledge_modes(workspace_dir: Path) -> list[str]:
    """Discover ingestable knowledge modes on the filesystem.

    A knowledge mode is any subdirectory of ``workspace/knowledge_modes/``
    that contains a ``MODE.md`` and a ``wiki/outputs/`` directory.
    """
    root = Path(workspace_dir) / "knowledge_modes"
    if not root.is_dir():
        return []
    modes: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "MODE.md").is_file():
            continue
        if not (entry / "wiki" / "outputs").is_dir():
            continue
        modes.append(entry.name)
    return modes


def iter_knowledge_mode_records(
    workspace_dir: Path,
    knowledge_base_id: str,
) -> Iterable[GraphSourceRecord]:
    """Yield ``GraphSourceRecord`` for every wiki/outputs/*.md page."""
    outputs = Path(workspace_dir) / "knowledge_modes" / knowledge_base_id / "wiki" / "outputs"
    if not outputs.is_dir():
        return
    for path in sorted(outputs.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        record = _record_from_mode_page(path, text, knowledge_base_id)
        if record is not None:
            yield record


def _record_from_mode_page(
    path: Path,
    text: str,
    knowledge_base_id: str,
) -> GraphSourceRecord | None:
    title = ""
    source_url = ""
    tags: list[str] = []
    ingested_at: str | None = None
    body_lines: list[str] = []
    in_summary = False
    for line in text.splitlines():
        if not title and line.startswith("# "):
            title = line[2:].strip()
            continue
        if not in_summary:
            match = _FRONTMATTER_RE.match(line)
            if match:
                key, value = match.group(1).strip(), match.group(2).strip()
                if key == "source_url":
                    source_url = value
                elif key == "tags":
                    tags = [tok.strip() for tok in re.split(r"[,，]", value) if tok.strip()]
                elif key == "ingested_at":
                    ingested_at = value
                continue
            if line.strip().startswith("## Summary"):
                in_summary = True
                continue
        else:
            body_lines.append(line)
    if not title:
        title = path.stem
    body = "\n".join(body_lines).strip()
    if not body:
        # No "## Summary" section — fall back to whole file (sans H1).
        body = "\n".join(
            line for line in text.splitlines() if not line.startswith("# ")
        ).strip()
    summary = body[:200] + ("…" if len(body) > 200 else "")
    return GraphSourceRecord(
        id=f"mode:{knowledge_base_id}:{path.stem}",
        source_type="paper",
        knowledge_base_id=knowledge_base_id,
        title=title,
        summary=summary,
        content=body,
        tags=tags,
        attrs={
            "source_url": source_url,
            "path": f"knowledge_modes/{knowledge_base_id}/wiki/outputs/{path.name}",
            "kind": "wiki_output",
        },
        chunks=_split_chunks(body),
        created_at=ingested_at,
    )
