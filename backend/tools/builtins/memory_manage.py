"""memory_manage: agent-facing CRUD for cross-session memory (v0.12 + v0.15).

Confirm-tier so every write is gated by the v0.6 IM yes/no flow — the
LLM cannot quietly seed its own system prompt without the operator's
sign-off. Seven actions:

* ``remember``    — add a new entry (gated by injection scanner)
* ``recall``      — search by query (read-only; the v0.9 review fork
                    invokes this freely via the trust-confirm bypass)
* ``forget``      — remove a single entry by id
* ``list``        — dump all active entries (optionally filtered by kind)
* ``pin``         — mark entry sacred (curator + autonomous code can't
                    touch it)
* ``unpin``       — release the lock
* ``consolidate`` — v0.15: archive near-duplicate entries (deterministic
                    similarity + provenance ranking, never touches
                    pinned / explicit-vs-review)

Tool description doubles as guidance to the LLM about *when* to write a
memory vs leaving the moment alone — the same conservative stance as
the v0.9 review prompt: "do nothing" is the most common right answer.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ...core.provenance import is_background_review
from ...memory.scanner import scan_content
from ...memory.store import (
    KIND_AGENT_NOTE,
    KIND_CONTROL_AXIOM,
    KIND_USER_FACT,
    SOURCE_EXPLICIT,
    SOURCE_REVIEW,
    VALID_KINDS,
    MemoryError,
    MemoryStore,
)
from ..base import Tool, ToolPermission, ToolResult

# v0.45 — performative-write patterns. These are the canonical shapes
# of "I'll do X / I won't do X / remember not to Y" lines that the LLM
# emits as a fake compliance gesture when the user complains about
# something. They don't describe a durable fact about the operator or
# environment, so saving them just bloats the snapshot block. The
# regex set is intentionally short + obvious — false positives are
# tolerable because the rejection message tells the LLM exactly how
# to rephrase ("write the underlying preference, not a promise").
_PERFORMATIVE_PATTERNS = [
    re.compile(r"已记住[，。,.\s]"),
    re.compile(r"不再(发|说|做|提)那"),
    re.compile(r"以后不再"),
    re.compile(r"^好的[，。,.\s]"),
    re.compile(r"^明白了[，。,.\s]"),
    re.compile(r"^我会(?:记住|尽量|努力)"),
    re.compile(r"^下次"),
    re.compile(r"^I will (?:remember|try|not|stop)", re.IGNORECASE),
    re.compile(r"^(?:Got it|OK|Sure)[,.\s]", re.IGNORECASE),
]


def _veto_performative_write(content: str) -> Optional[str]:
    """Return a rejection reason iff ``content`` looks like a performative
    acknowledgement rather than a durable fact."""
    if not content:
        return None
    for pattern in _PERFORMATIVE_PATTERNS:
        if pattern.search(content):
            return (
                "content reads like a performative acknowledgement"
                " ('已记住 / 不再发 / 好的 / I will remember ...') rather"
                " than a durable fact about the operator or environment."
                " Memory is a fact ledger, not a compliance log — write"
                " the underlying preference instead (e.g. '用户希望 ack"
                " 简短，不要重复确认' rather than '已记住，不再发那句')."
                " If there is no durable preference to record, simply"
                " don't write to memory this turn."
            )
    return None

# Tool-level write whitelist. Agent / review fork can write into L2
# (agent_note) and L3 (user_fact); L1 (control_axiom) is system-managed
# — only ``backend.memory.bootstrap.seed_control_axioms`` and the
# operator REST surface can mutate it. This guarantees the agent
# can't quietly rewrite its own thinking doctrine mid-conversation.
TOOL_WRITABLE_KINDS = frozenset({KIND_USER_FACT, KIND_AGENT_NOTE})


class MemoryManageTool(Tool):
    name = "memory_manage"
    description = (
        "Manage cross-session memory about the operator and the agent's"
        " own observations. Three semantic layers (钱学森三层递阶记忆体)"
        " — the agent can only write into L2 and L3:\n\n"
        "* ``control_axiom`` (L1, system-managed, **read-only here**) —"
        " control-theory base logic (goal / state / deviation / feedback"
        " / execution / correction). Seeded once by bootstrap from"
        " ``DEFAULT_CONTROL_AXIOMS``; surfaces at the top of every system"
        " prompt. The agent cannot write to this layer through this tool"
        " — operators curate it via REST so the agent can never quietly"
        " rewrite its own thinking doctrine.\n"
        "* ``agent_note`` (L2, abstracted underlying logic) — the"
        " agent's own working notes: **trigger conditions / judgment"
        " criteria / failure signals / environment quirks**. NOT a"
        " place for chat log details. Examples:"
        " '本机时区是 Asia/Shanghai (env)';"
        " 'trigger=用户问 arxiv 论文 → criterion=2-3 轮工具调用后停 →"
        " failure=死循环刷新';"
        " 'arxiv API 偶发 429，重试间隔 60s 起步 (env quirk)'.\n"
        "* ``user_fact`` (L3, scenario-grounded) — facts about the"
        " operator: preferences, names, relationships, recurring goals."
        " Examples: '老婆叫小明，生日 6 月 15';"
        " '我喜欢回复简短，不超过 50 字';"
        " '我在做一个叫 ZLAgent 的项目'.\n\n"
        "Permission tier is **safe** for personal-AI use — writes go"
        " through without an IM yes/no. The operator owns the data and"
        " expects the agent to remember corrections immediately (e.g.,"
        " '我叫 ZL，别再叫小明' should land in memory on the next"
        " message, not after a confirmation round-trip). Be conservative"
        " about WHAT you remember (see the rules below), not about"
        " whether to ask."
        "\n\n"
        "**Memory model (钱学森工程控制论)**:\n"
        "  1. First layer: control-theory base logic — goal, state,"
        " deviation, feedback, execution, correction, constraints, signals.\n"
        "  2. Second layer: abstract each dialogue into underlying logic,"
        " trigger conditions, judgment criteria, and failure signals; do"
        " not store one-off chat details.\n"
        "  3. Third layer: keep only reusable scenario logic that can"
        " trigger a matching skill or tool when work starts.\n\n"
        "**WHEN TO SAVE** (proactive — don't wait to be asked twice."
        " The most valuable memory prevents the user from having to"
        " repeat themselves):\n"
        "* **Identity / corrections** (almost always write):"
        " '我叫 ZL 不叫小明' / '我其实不叫 X' / 'my name is ...'.\n"
        "* **Long-term rules**:"
        " '以后 / 之后都 / 默认 / 每次 / from now on / always' + behaviour.\n"
        "* **Hard nos**:"
        " '别再 ... / 不要再 ... / stop doing ... / never call me ...'.\n"
        "* **Preferences**:"
        " '我喜欢 ... / 我习惯 ... / 我倾向 ... / I prefer / I hate'.\n"
        "* **Relationships**:"
        " '我老婆叫 X / 我家有只猫叫毛球 / my wife / my dog ...'.\n"
        "* **Time / locale**:"
        " '我在东八区 / 北京时间 / I'm in PST'.\n"
        "* **Project / work context**:"
        " '我在做 X 项目 / 我用 Rust / my project is ...'.\n"
        "* **Recurring schedule**:"
        " '每天 8 点 / 每周一 / every weekday'.\n"
        "* **Environment facts you discovered**:"
        " timezone, API rate limits, tool quirks, file path conventions.\n\n"
        "**PRIORITY**: identity / corrections > long-term rules >"
        " preferences > relationships > project context > environment."
        " The most valuable memory prevents the user from having to"
        " repeat themselves.\n\n"
        "**When NOT to remember**:\n"
        "* The fact is already obvious from the system prompt or skills.\n"
        "* It's a one-off detail (today's specific weather, this turn's"
        " random number).\n"
        "* It's session-internal context (\"we're in the middle of"
        " refactoring file X\") — that belongs in the LLM's working"
        " memory, not durable storage.\n"
        "* The user explicitly said 不用记 / 临时的 / 一次性 / just for now.\n"
        "* It is merely a successful routine execution with no reusable"
        " trigger logic, failure signal, or judgment criterion.\n\n"
        "**Refusal patterns** — the underlying scanner refuses content"
        " that looks like prompt-injection or credential-exfiltration"
        " (e.g. 'ignore previous instructions', 'curl ... $API_KEY')."
        " If a write is refused, rephrase to drop the trigger phrase."
    )
    permission = ToolPermission.SAFE
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = True
    max_result_chars = 8_000
    search_hint = "memory remember recall list forget pin consolidate user facts notes"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "remember",
                    "recall",
                    "forget",
                    "list",
                    "pin",
                    "unpin",
                    "consolidate",
                ],
                "description": "Which CRUD operation to perform.",
            },
            "kind": {
                "type": "string",
                "enum": ["user_fact", "agent_note"],
                "description": (
                    "Required for ``remember``; optional filter for"
                    " ``list`` / ``recall``. Pick ``user_fact`` for"
                    " anything about the operator; ``agent_note`` for"
                    " the agent's own working notes."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "The memory text. Required for ``remember``. Keep it"
                    " short (a single sentence ≤500 chars) and self-"
                    " contained — the entry will be quoted in future"
                    " system prompts as-is."
                ),
            },
            "memory_id": {
                "type": "integer",
                "description": (
                    "Required for ``forget`` / ``pin`` / ``unpin``. The"
                    " integer id from a prior ``list`` or ``recall``"
                    " result. Never guess — call ``list`` first."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Substring used by ``recall`` (case-insensitive,"
                    " whitespace-tokenised, OR'd). Empty means 'top"
                    " recent entries'."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Cap on rows returned by list / recall.",
                "default": 20,
            },
            "similarity_threshold": {
                "type": "number",
                "minimum": 0.5,
                "maximum": 1.0,
                "description": (
                    "Used by ``consolidate``. SequenceMatcher ratio"
                    " threshold above which two same-kind, non-pinned"
                    " entries are merged. Defaults to 0.80 — matches"
                    " the skill curator's deduplicate threshold (0.70)"
                    " plus a margin because memory is shorter and the"
                    " ratio is more sensitive on short strings."
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        store: MemoryStore,
        memory_manager: Optional[Any] = None,
        *,
        max_fact_chars: int = 280,
    ) -> None:
        self._store = store
        self._memory_manager = memory_manager
        # v0.45 — hard cap on a single fact entry. Hermes' built-in
        # curator keeps each MEMORY.md / USER.md line under Twitter
        # length so the snapshot fits in the system prompt cheaply.
        # Writes longer than this get rejected with a clear hint to
        # summarise; this prevents the "agent saved my whole 7-day
        # itinerary as a fact" failure mode.
        self._max_fact_chars = max(60, int(max_fact_chars))

    # =================================================================
    # entry point
    # =================================================================

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        if not action:
            return ToolResult(ok=False, content="", error="action is required")
        try:
            if action == "remember":
                return self._remember(arguments)
            if action == "recall":
                return self._recall(arguments)
            if action == "forget":
                return self._forget(arguments)
            if action == "list":
                return self._list(arguments)
            if action == "pin":
                return self._set_pinned(arguments, pinned=True)
            if action == "unpin":
                return self._set_pinned(arguments, pinned=False)
            if action == "consolidate":
                return self._consolidate(arguments)
        except MemoryError as exc:
            return ToolResult(ok=False, content="", error=str(exc))
        return ToolResult(ok=False, content="", error=f"unknown action: {action}")

    # =================================================================
    # action handlers
    # =================================================================

    def _remember(self, arguments: dict[str, Any]) -> ToolResult:
        kind = str(arguments.get("kind") or "").strip()
        if not kind:
            return ToolResult(
                ok=False, content="",
                error="kind is required for remember (user_fact | agent_note)",
            )
        if kind == KIND_CONTROL_AXIOM:
            # L1 is system-managed. Refuse with a clear pointer to the
            # right action ("write at L2 if you noticed a new principle").
            return ToolResult(
                ok=False, content="",
                error=(
                    "kind 'control_axiom' (L1) is system-managed and"
                    " cannot be written via memory_manage. If you spotted"
                    " a new underlying principle worth promoting, write"
                    " it at L2 as kind='agent_note' first; an operator"
                    " can promote it to L1 via REST."
                ),
            )
        if kind not in TOOL_WRITABLE_KINDS:
            return ToolResult(
                ok=False, content="",
                error=(
                    f"invalid kind {kind!r}; must be one of"
                    f" {sorted(TOOL_WRITABLE_KINDS)} (control_axiom is"
                    " system-managed)"
                ),
            )
        content = str(arguments.get("content") or "")
        if not content.strip():
            return ToolResult(
                ok=False, content="", error="content is required for remember",
            )
        # Injection / exfiltration scan happens BEFORE the store write
        # so the rejection message comes from the scanner module — easier
        # to read than a generic "MemoryError" wrapper.
        veto = scan_content(content)
        if veto is not None:
            return ToolResult(ok=False, content="", error=veto)

        # v0.45 — Hermes-style brevity guard. The user explicitly asked
        # that we "save only the place feature, not my whole 7-day plan"
        # — so we hard-cap a fact at ``max_fact_chars`` and reject longer
        # writes with a hint to summarise. This catches both "整份规划"
        # itinerary blobs and runaway log-style notes.
        stripped = content.strip()
        if len(stripped) > self._max_fact_chars:
            return ToolResult(
                ok=False, content="",
                error=(
                    f"content too long ({len(stripped)} > {self._max_fact_chars}"
                    " chars). Memory entries are short fact cards"
                    " (Hermes-style), not paragraphs / plans / itineraries."
                    " Compress to one sentence stating the durable fact"
                    " (e.g. '北京三日游偏好故宫+长城+颐和园路线' instead"
                    " of the full Day-by-Day breakdown), and re-call"
                    " remember. Use skill_manage if you need to save a"
                    " reusable procedure."
                ),
            )
        # v0.45 — performative-write guard. Reject contents that look
        # like the LLM is "acknowledging" an instruction rather than
        # saving a durable fact (e.g. user says "别再发那句话了" and
        # the bot writes 'remember: 已记住，不再发那句'). These entries
        # bloat the snapshot without changing behaviour because the
        # actual control surface for that behaviour is code, not memory.
        veto_perf = _veto_performative_write(stripped)
        if veto_perf is not None:
            return ToolResult(ok=False, content="", error=veto_perf)

        # Tag provenance from the active write origin so future curator-
        # style sweeps can distinguish operator-blessed entries from
        # the review fork's autonomous additions.
        source = SOURCE_REVIEW if is_background_review() else SOURCE_EXPLICIT
        knowledge_base_id = self._infer_knowledge_base_id(
            kind=kind,
            content=stripped,
            source=source,
        )
        row = self._store.add(
            content,
            kind=kind,
            source=source,
            knowledge_base_id=knowledge_base_id,
        )
        self._notify_memory_write("add", kind, row["content"], row)
        return ToolResult(
            ok=True,
            content=(
                f"Memory #{row['id']} created (kind={row['kind']},"
                f" source={row['source']}). It will be visible from the"
                f" next turn's system prompt onward."
            ),
        )

    def _recall(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "")
        limit = self._normalise_limit(arguments.get("limit"))
        if isinstance(limit, ToolResult):
            return limit
        results = self._store.search(query, limit=limit)
        if not results:
            return ToolResult(ok=True, content="(no matching memories)")
        lines = [self._format_row(r) for r in results]
        return ToolResult(ok=True, content="\n".join(lines))

    def _list(self, arguments: dict[str, Any]) -> ToolResult:
        kind = arguments.get("kind")
        if isinstance(kind, str) and kind.strip():
            kind = kind.strip()
            if kind not in VALID_KINDS:
                return ToolResult(
                    ok=False, content="",
                    error=f"invalid kind {kind!r}; must be one of {sorted(VALID_KINDS)}",
                )
        else:
            kind = None
        limit = self._normalise_limit(arguments.get("limit"))
        if isinstance(limit, ToolResult):
            return limit
        results = self._store.list(kind=kind, limit=limit)
        if not results:
            return ToolResult(ok=True, content="(no memories yet)")
        lines = [self._format_row(r) for r in results]
        # Append an aggregate footer so the LLM can quickly see the
        # remaining budget without a separate ``stats`` call.
        stats = self._store.stats()
        lines.append(
            f"\n[total={stats['total']} active={stats['active']} pinned={stats['pinned']}]"
        )
        return ToolResult(ok=True, content="\n".join(lines))

    def _forget(self, arguments: dict[str, Any]) -> ToolResult:
        memory_id = arguments.get("memory_id")
        if not isinstance(memory_id, int):
            return ToolResult(
                ok=False, content="", error="memory_id (int) required for forget",
            )
        row = self._store.get(memory_id)
        try:
            removed = self._store.remove(memory_id)
        except MemoryError as exc:
            return ToolResult(ok=False, content="", error=str(exc))
        if not removed:
            return ToolResult(
                ok=False, content="", error=f"memory #{memory_id} not found",
            )
        self._notify_memory_write(
            "remove",
            str(row.get("kind") if row else "memory"),
            str(row.get("content") if row else memory_id),
            {"id": memory_id, "row": row},
        )
        return ToolResult(ok=True, content=f"Memory #{memory_id} forgotten.")

    def _set_pinned(self, arguments: dict[str, Any], *, pinned: bool) -> ToolResult:
        memory_id = arguments.get("memory_id")
        if not isinstance(memory_id, int):
            return ToolResult(
                ok=False, content="",
                error=f"memory_id (int) required for {'pin' if pinned else 'unpin'}",
            )
        ok = self._store.set_pinned(memory_id, pinned)
        if not ok:
            return ToolResult(
                ok=False, content="", error=f"memory #{memory_id} not found",
            )
        row = self._store.get(memory_id)
        self._notify_memory_write(
            "pin" if pinned else "unpin",
            str(row.get("kind") if row else "memory"),
            str(row.get("content") if row else memory_id),
            {"id": memory_id, "pinned": pinned, "row": row},
        )
        verb = "pinned" if pinned else "unpinned"
        return ToolResult(ok=True, content=f"Memory #{memory_id} {verb}.")

    def _consolidate(self, arguments: dict[str, Any]) -> ToolResult:
        """Trigger a deterministic merge sweep over near-duplicate entries.

        The actual policy lives in :meth:`MemoryStore.consolidate` so the
        REST surface and the review fork agree on behaviour. We only
        validate / surface the output here.
        """
        kind = arguments.get("kind")
        if isinstance(kind, str) and kind.strip():
            kind = kind.strip()
            if kind not in VALID_KINDS:
                return ToolResult(
                    ok=False, content="",
                    error=f"invalid kind {kind!r}; must be one of {sorted(VALID_KINDS)}",
                )
        else:
            kind = None
        threshold_raw = arguments.get("similarity_threshold")
        if threshold_raw is None:
            threshold = 0.80
        else:
            try:
                threshold = float(threshold_raw)
            except (TypeError, ValueError):
                return ToolResult(
                    ok=False, content="",
                    error=f"similarity_threshold must be number, got {threshold_raw!r}",
                )
            if not 0.5 <= threshold <= 1.0:
                return ToolResult(
                    ok=False, content="",
                    error="similarity_threshold must be between 0.5 and 1.0",
                )
        report = self._store.consolidate(
            kind=kind, similarity_threshold=threshold,
        )
        details = report.get("details") or []
        if not details:
            return ToolResult(
                ok=True,
                content=(
                    f"Consolidation reviewed {report['pairs_considered']}"
                    f" candidate pair(s) at threshold ≥ {report['similarity_threshold']};"
                    " nothing close enough to merge."
                ),
            )
        lines = [
            f"Consolidation merged {len(details)} pair(s)"
            f" (threshold ≥ {report['similarity_threshold']}):"
        ]
        for d in details:
            lines.append(
                f"  • #{d['archived_id']} ({d['archived_source']})"
                f" → #{d['survivor_id']} ({d['survivor_source']})"
                f"  sim={d['similarity']:.2f} kind={d['kind']}"
            )
        return ToolResult(ok=True, content="\n".join(lines))

    # =================================================================
    # helpers
    # =================================================================

    @staticmethod
    def _normalise_limit(raw: Any) -> int | ToolResult:
        if raw is None:
            return 20
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return ToolResult(ok=False, content="", error=f"limit must be int, got {raw!r}")
        if not 1 <= value <= 50:
            return ToolResult(
                ok=False, content="", error="limit must be between 1 and 50",
            )
        return value

    def _notify_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        manager = self._memory_manager
        if manager is None:
            return
        try:
            manager.on_memory_write(action, target, content, metadata=metadata)
        except Exception:
            return

    def _infer_knowledge_base_id(
        self,
        *,
        kind: str,
        content: str,
        source: str,
    ) -> str:
        text = content.lower()
        if kind == KIND_USER_FACT:
            if any(token in text for token in ("论文", "arxiv", "paper", "llm", "rag", "agent")):
                return "paper"
            if any(token in text for token in ("旅游", "旅行", "酒店", "机票", "景点", "攻略")):
                return "travel"
        if kind == KIND_AGENT_NOTE and source == SOURCE_REVIEW:
            if any(token in text for token in ("论文", "arxiv", "paper", "llm", "rag", "agent")):
                return "paper"
            if any(token in text for token in ("旅游", "旅行", "酒店", "机票", "景点", "攻略")):
                return "travel"
        return "default"

    @staticmethod
    def _format_row(row: dict[str, Any]) -> str:
        marker = "📌 " if row.get("pinned") else ""
        base = row.get("knowledge_base_id") or "default"
        return (
            f"#{row['id']} [{base}|{row['kind']}|{row['source']}|recall={row['recall_count']}]"
            f" {marker}{row['content']}"
        )
