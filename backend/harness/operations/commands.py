"""IM slash-command handlers for the harness layer.

Recognises a small fixed vocabulary the user can type to manage their
extensions without leaving chat:

* ``/list`` (alias ``/extensions``) — show installed user extensions.
* ``/list <kind>`` — narrow to ``skills`` / ``mcps`` / ``plugins`` /
  ``tools``.
* ``/remove <name>`` — uninstall a user extension; auto-detects kind
  by scanning the inventory.
* ``/remove <kind> <name>`` — explicit kind for ambiguous names.
* ``/help`` — short usage cheat sheet.

The handler is intentionally synchronous-friendly (returns ``None`` for
unrecognised input) so call sites can short-circuit before the LLM
turn. Removal is async because it delegates to ``skill_manage`` /
``MCPLifecycleService`` which are coroutines.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from .. import HarnessInventory
from ..extensions.lifecycle import (
    UninstallOutcome,
    uninstall_mcp,
    uninstall_plugin,
    uninstall_skill,
    uninstall_tool,
)

if TYPE_CHECKING:  # pragma: no cover
    from .. import Harness


_LIST_ALIASES = ("/list", "/extensions", "/ls")
_REMOVE_ALIASES = ("/remove", "/uninstall", "/rm")
_HELP_ALIASES = ("/help", "/?")
# v0.43 Phase B+ — daily-review audit + rollback via IM.
_REVIEW_ALIASES = ("/review", "/rv")

_KIND_ALIASES: dict[str, str] = {
    "skill": "skill",
    "skills": "skill",
    "mcp": "mcp",
    "mcps": "mcp",
    "plugin": "plugin",
    "plugins": "plugin",
    "tool": "tool",
    "tools": "tool",
}


def _split(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def _is_command(text: str) -> bool:
    """Return True if text starts with a recognised command token."""
    head, _ = _split(text)
    return head in (
        _LIST_ALIASES + _REMOVE_ALIASES + _HELP_ALIASES + _REVIEW_ALIASES
    )


# ---------------------------------------------------------------------------
# /list formatting
# ---------------------------------------------------------------------------


def _fmt_skill(row: dict) -> str:
    desc = (row.get("description") or "").strip().splitlines()
    summary = desc[0] if desc else ""
    return f"  • {row['id']}  {summary}".rstrip()


def _fmt_mcp(row: dict) -> str:
    state = "online" if row.get("connected") else "offline"
    n_tools = row.get("tool_count", 0)
    return (
        f"  • {row['name']}  [{row.get('transport', '')}, {state}, {n_tools} tools]"
        f"  {row.get('description', '')}".rstrip()
    )


def _fmt_plugin(row: dict) -> str:
    status = row.get("status", "")
    return f"  • {row['id']}  v{row.get('version', '?')}  [{status}]".rstrip()


def _fmt_tool(row: dict) -> str:
    return (
        f"  • {row['name']}  [{row.get('origin', '?')}]"
        f"  {row.get('description', '')}".rstrip()
    )


def _format_list(inv: HarnessInventory, *, kind: Optional[str] = None) -> str:
    """Build the IM-facing inventory listing.

    Always includes the core summary footer so the user sees that core
    capabilities are engaged even when their extension list is empty.
    """
    blocks: list[str] = []
    show_skills = kind is None or kind == "skill"
    show_mcps = kind is None or kind == "mcp"
    show_plugins = kind is None or kind == "plugin"
    show_tools = kind is None or kind == "tool"

    if show_skills:
        blocks.append(f"已安装的技能 ({len(inv.skills)}):")
        if inv.skills:
            blocks.extend(_fmt_skill(s) for s in inv.skills)
        else:
            blocks.append("  (无)")

    if show_mcps:
        blocks.append(f"\nMCP 服务器 ({len(inv.mcps)}):")
        if inv.mcps:
            blocks.extend(_fmt_mcp(m) for m in inv.mcps)
        else:
            blocks.append("  (无)")

    if show_plugins:
        blocks.append(f"\n插件 ({len(inv.plugins)}):")
        if inv.plugins:
            blocks.extend(_fmt_plugin(p) for p in inv.plugins)
        else:
            blocks.append("  (无)")

    if show_tools:
        blocks.append(f"\n扩展工具 ({len(inv.tools)}):")
        if inv.tools:
            blocks.extend(_fmt_tool(t) for t in inv.tools)
        else:
            blocks.append("  (无)")

    summary = inv.core_summary
    blocks.append(
        f"\n核心能力: {summary.get('core_skill_catalog_size', 0)} 个内置技能"
        f" + {summary.get('tools_hidden', 0)} 个内置工具 (已隐藏)。"
    )
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# /remove resolution
# ---------------------------------------------------------------------------


def _detect_kind(inv: HarnessInventory, name: str) -> Optional[str]:
    """Search the inventory for ``name``; return its kind or ``None``."""
    if any(s["id"] == name or s.get("name") == name for s in inv.skills):
        return "skill"
    if any(m["name"] == name for m in inv.mcps):
        return "mcp"
    if any(p["id"] == name for p in inv.plugins):
        return "plugin"
    if any(t["name"] == name for t in inv.tools):
        return "tool"
    return None


def _format_uninstall(outcome: UninstallOutcome) -> str:
    if outcome.ok:
        return f"✅ 已删除 {outcome.kind} '{outcome.name}'。{outcome.message}".strip()
    if outcome.refused_reason == "core":
        return f"❌ {outcome.message}"
    if outcome.refused_reason == "not_found":
        return f"❌ 未找到 {outcome.kind} '{outcome.name}'。{outcome.message}".strip()
    if outcome.refused_reason == "missing_subsystem":
        return f"❌ {outcome.kind} 子系统未启用：{outcome.message}"
    if outcome.refused_reason == "unsupported":
        return f"❌ {outcome.message}"
    return f"❌ 删除失败：{outcome.message}"


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


HELP_TEXT = (
    "harness 命令：\n"
    "  /list                查看已安装的所有用户扩展\n"
    "  /list skills|mcps|plugins|tools    只看一类\n"
    "  /remove <name>       删除一个扩展（自动判断类型）\n"
    "  /remove <kind> <name>  显式指定类型\n"
    "  /review              日终复盘（别名 /rv）\n"
    "  /review last         上一轮复盘的 summary\n"
    "  /review log [N]      最近 N 条复盘记录（默认 5）\n"
    "  /review rollback <run_id>   回滚某次复盘的 memory 改动\n"
    "  /review run          立刻跑一次复盘\n"
    "  /help                显示这份帮助\n"
    "核心能力对用户隐藏、不可删除。"
)


# ---------------------------------------------------------------------------
# /review formatting
# ---------------------------------------------------------------------------


def _fmt_review_last(svc) -> str:
    """Render ``svc.last_summary`` as a readable IM block.

    ``svc`` is a :class:`DailyReviewService`. Kept loosely typed on
    purpose so the harness doesn't import a Phase B class just to
    satisfy the type checker.
    """
    if svc is None:
        return "❌ 日终复盘服务未启用"
    summary = svc.last_summary
    if summary is None:
        return "ℹ 还没有复盘记录。用 /review run 立刻跑一次。"
    inputs = summary.get("inputs", {}) or {}
    review = summary.get("review", {}) or {}
    actions = summary.get("actions", {}) or {}
    ok = bool(review.get("ok"))
    lines = [f"[日终复盘] {summary.get('finished_at', '?')}"]
    lines.append(
        f"  输入: notes={inputs.get('agent_note_count', 0)}"
        f" facts={inputs.get('user_fact_count', 0)}"
        f" skill_evts={inputs.get('skill_history_count', 0)}"
        f" 热门 skill={inputs.get('skills_with_usage', 0)}"
    )
    if not ok:
        reason = review.get("reason") or "unknown error"
        lines.append(f"  ⚠ 复盘未执行: {reason}")
        return "\n".join(lines)
    skill_calls = int(review.get("skill_calls") or 0)
    memory_calls = int(review.get("memory_calls") or 0)
    run_id = actions.get("run_id")
    if skill_calls == 0 and memory_calls == 0:
        lines.append("  ✔ 无需更新")
        return "\n".join(lines)
    lines.append(
        f"  ✔ skill_calls={skill_calls} memory_calls={memory_calls}"
        + (f" run_id={run_id}" if run_id is not None else "")
    )
    final_text = (review.get("final_text") or "").strip()
    if final_text:
        if len(final_text) > 400:
            final_text = final_text[:399] + "…"
        lines.append(f"  → {final_text}")
    new_ids = actions.get("new_memory_ids") or []
    arch_ids = actions.get("archived_memory_ids") or []
    if new_ids or arch_ids:
        lines.append(
            f"  memory: 新增 {new_ids}  归档 {arch_ids}"
        )
    added_count = int(actions.get("skill_history_added_count") or 0)
    if added_count:
        lines.append(f"  skill_history +{added_count} 条")
    return "\n".join(lines)


def _fmt_review_log(svc, limit: int) -> str:
    """Render the audit log as a compact list (newest first)."""
    if svc is None or svc.action_log is None:
        return "❌ 复盘审计日志未启用"
    runs = svc.action_log.list_runs(limit=max(1, limit))
    if not runs:
        return "ℹ 还没有复盘记录。"
    lines = [f"[最近 {len(runs)} 条复盘] 新→旧"]
    for r in runs:
        rid = r.get("run_id")
        finished = r.get("finished_at") or r.get("started_at") or "?"
        sc = r.get("skill_calls") or 0
        mc = r.get("memory_calls") or 0
        nm = len(r.get("new_memory_ids") or [])
        am = len(r.get("archived_memory_ids") or [])
        sh = len(r.get("skill_history_added") or [])
        rb = r.get("rolled_back_at")
        marker = " [已回滚]" if rb else ""
        lines.append(
            f"  #{rid}{marker}  {finished}"
            f"  skill={sc} memory={mc} new_mem={nm} arch={am} skill_evts={sh}"
        )
    lines.append("  /review rollback <run_id> 撤销某次的 memory 改动")
    return "\n".join(lines)


def _fmt_review_rollback(result: dict) -> str:
    if not result.get("ok"):
        return f"❌ 回滚失败: {result.get('reason', 'unknown')}"
    rid = result.get("run_id")
    arch = result.get("memory_archived") or []
    unarch = result.get("memory_unarchived") or []
    skill_added = result.get("skill_history_added") or []
    lines = [f"✅ 已回滚 #{rid} 的 memory 改动"]
    if arch:
        lines.append(f"  archived (撤销新增): {arch}")
    if unarch:
        lines.append(f"  unarchived (恢复): {unarch}")
    if not arch and not unarch:
        lines.append("  (没有 memory 改动需要回滚)")
    if skill_added:
        lines.append(
            f"  ⚠ 本次复盘还有 {len(skill_added)} 条 skill_history；"
            "skill 文件不自动回滚, 需要 git revert 或手动编辑。"
        )
    errors = result.get("errors") or []
    if errors:
        lines.append(f"  errors: {'; '.join(errors)}")
    return "\n".join(lines)


async def handle_command(text: str, harness: "Harness") -> Optional[str]:
    """Dispatch ``text`` to the matching handler.

    Returns the reply string for the IM gateway, or ``None`` if the
    text is not a recognised harness command (caller should fall
    through to the normal agent turn).
    """
    head, args = _split(text)
    if not head:
        return None

    if head in _HELP_ALIASES:
        return HELP_TEXT

    if head in _LIST_ALIASES:
        kind: Optional[str] = None
        if args:
            mapped = _KIND_ALIASES.get(args[0].lower())
            if mapped is None:
                return f"❌ 未知类别 '{args[0]}'。可用：skills / mcps / plugins / tools。"
            kind = mapped
        return _format_list(harness.inventory(), kind=kind)

    if head in _REMOVE_ALIASES:
        if not args:
            return "❌ 用法：/remove <name>  或  /remove <kind> <name>"
        # Two-arg form?
        kind: Optional[str] = None
        target_name: str
        if len(args) >= 2 and args[0].lower() in _KIND_ALIASES:
            kind = _KIND_ALIASES[args[0].lower()]
            target_name = " ".join(args[1:]).strip()
        else:
            target_name = " ".join(args).strip()
        if not target_name:
            return "❌ 缺少要删除的扩展名"
        if kind is None:
            inv = harness.inventory()
            kind = _detect_kind(inv, target_name)
            if kind is None:
                # Nothing matched — could be a core item or typo. Refuse
                # gracefully without leaking core names.
                return (
                    f"❌ 在已安装的扩展里找不到 '{target_name}'。"
                    f" 用 /list 看看现在装了什么；如果是核心能力，"
                    f"它对外是隐藏且不可删除的。"
                )
        # Dispatch
        if kind == "skill":
            outcome = await uninstall_skill(harness, target_name)
        elif kind == "mcp":
            outcome = await uninstall_mcp(harness, target_name)
        elif kind == "plugin":
            outcome = await uninstall_plugin(harness, target_name)
        elif kind == "tool":
            outcome = await uninstall_tool(harness, target_name)
        else:
            return f"❌ 不支持的类别 '{kind}'"
        return _format_uninstall(outcome)

    if head in _REVIEW_ALIASES:
        svc = getattr(harness, "daily_review_service", None)
        sub = (args[0].lower() if args else "last")
        if sub in ("last", "latest", "show"):
            return _fmt_review_last(svc)
        if sub in ("log", "ls", "list"):
            # /review log [N]
            limit = 5
            if len(args) >= 2:
                try:
                    limit = max(1, min(50, int(args[1])))
                except ValueError:
                    return "❌ 用法：/review log [N]   N 是 1~50 的整数"
            return _fmt_review_log(svc, limit)
        if sub in ("rollback", "undo", "revert"):
            if svc is None or getattr(svc, "action_log", None) is None:
                return "❌ 复盘审计日志未启用"
            if len(args) < 2:
                return "❌ 用法：/review rollback <run_id>"
            try:
                run_id = int(args[1])
            except ValueError:
                return f"❌ run_id 必须是整数，收到 {args[1]!r}"
            result = svc.rollback_run(run_id, note="via IM /review rollback")
            return _fmt_review_rollback(result)
        if sub in ("run", "now", "fire"):
            if svc is None:
                return "❌ 日终复盘服务未启用"
            summary = await svc.run_once()
            if summary is None:
                return "❌ 复盘执行失败（详见服务日志）"
            return _fmt_review_last(svc)
        return (
            f"❌ 未知的 /review 子命令 '{sub}'。"
            f" 支持：last | log [N] | rollback <id> | run"
        )

    return None


__all__ = [
    "HELP_TEXT",
    "handle_command",
    # exposed for unit tests
    "_detect_kind",
    "_format_list",
    "_format_uninstall",
    "_fmt_review_last",
    "_fmt_review_log",
    "_fmt_review_rollback",
    "_is_command",
]
