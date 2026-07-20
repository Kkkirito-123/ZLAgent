"""ZLAgent — Self-improving IM Agent — End-to-End Demo.

Runs the 4-turn narrative that backs the project's positioning:

    ① wiki cache miss     → ~slow path     (first time you ask)
    ② wiki cache hit      → ~fast path     (paraphrase the same question)
    ③ MCP auto-discovery  → agent proposes installing a missing tool
    ④ proactive memory    → 'remember' intent fires WITHOUT a /command

All LLM calls are stubbed with deterministic responses so the demo runs
fully offline. Cache, skill router, memory intent, tool registry, and
write-back are real ZLAgent code paths — only the model is faked.

Usage:
    python scripts/demo_e2e.py

Output is plain UTF-8, suitable for a terminal recording (asciinema /
`gif`-friendly tools). Each turn is a clearly-delineated block so a
reader can follow the agent's behaviour at a glance.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# Match smoke's bootstrap so the demo runs offline and against a fresh
# per-process SQLite path. Pop any leaked OpenAI env so the real LLM
# wiring stays asleep.
os.environ.setdefault("ZLAGENT_DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("ZLAGENT_CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("ZLAGENT_WORKSPACE_DIR", str(ROOT / "workspace"))
os.environ.setdefault("ZLAGENT_MCP_ENABLED", "false")
os.environ.setdefault(
    "ZLAGENT_DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'zlagent.db'}",
)
for _env_key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY"):
    os.environ.pop(_env_key, None)
os.environ["OPENAI_BASE_URL"] = "https://api.openai.com/v1"

from backend.core.config import Settings as _S  # noqa: E402

_S.model_config["env_file"] = None

from datetime import datetime, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from backend.app import app  # noqa: E402
from backend.gateways.base import (  # noqa: E402
    DeliveryTarget,
    IncomingMessage,
)
from backend.llm.openai_compatible import LLMResponse, LLMToolCall  # noqa: E402
from backend.memory.intent import detect_memory_intent  # noqa: E402


# =============================================================================
# Scripted LLM — deterministic responses keyed off the last user message
# =============================================================================

ITINERARY = (
    "北京 · 3日深度路线\n"
    "Day 1: 故宫(上午) → 王府井(下午) → 簋街宵夜(晚)\n"
    "Day 2: 八达岭长城 + 鸟巢/水立方\n"
    "Day 3: 颐和园 → 798艺术区\n"
    "预算: 餐饮 200-400/日，门票 200-300/日，交通 50-100/日\n"
    "Tips: 故宫提前 7 天预约；长城返程坐 S2 比自驾快。"
)

MCP_PROPOSAL = (
    "我目前没有订火车票的工具。搜了 MCP 注册表，给你 3 个候选：\n"
    "\n"
    "  1. 12306-mcp     — 查余票/价格 (爬取 12306 公开页)\n"
    "                    install: npx -y @example/12306-mcp\n"
    "  2. ctrip-mcp     — 携程公开 API，火车/机票/酒店都能查\n"
    "                    install: npx -y @example/ctrip-mcp\n"
    "  3. trip-cn-mcp   — 同程，仅查询不下单\n"
    "                    install: npx -y @example/trip-cn-mcp\n"
    "\n"
    "选哪一个? 回 1 / 2 / 3 / 否。装上需要 confirm。"
)

MEMORY_ACK = "好的，已记住您的出行偏好。下次规划差旅会优先推荐高铁。"
DEMO_MEMORY = "用户默认选择高铁出行，不坐飞机。"


class ScriptedLLM:
    """Deterministic LLM stand-in. Returns canned text per turn topic.

    The agent loop calls ``chat(messages)`` for the main turn and again
    for the post-turn review fork. We dispatch by content sniffing the
    last user message. The memory-review fork emits a real
    ``memory_manage(remember)`` tool call, so the demo verifies durable state
    rather than merely printing a claim.

    Latency simulation: each canned response includes a ``simulated_ms``
    sleep that mimics a real provider's first-byte time. Without it the
    demo's "speedup" headline is meaningless because cache-hit fast path
    measured against a 0-latency fake LLM gives a useless 1×. The numbers
    chosen are conservative for OpenAI gpt-4o-mini / DeepSeek-V3 over a
    domestic network: ~2-3s for a 600-char reply, ~800ms-1.5s for a
    200-char reply.
    """

    # Tunable for CI: set ZLAGENT_DEMO_FAST=1 to skip the simulated
    # latency entirely (smoke runs the demo as a sanity check and
    # doesn't want to wait 4+ seconds for the latency dramatization).
    _FAST = bool(int(os.environ.get("ZLAGENT_DEMO_FAST", "0") or "0"))

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    @property
    def model(self) -> str:
        return "scripted-demo-llm"

    @property
    def provider(self) -> str:
        return "scripted-demo"

    async def _simulate(self, ms: int) -> None:
        if self._FAST or ms <= 0:
            return
        await asyncio.sleep(ms / 1000)

    async def chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        # Sniff: is this a review-fork invocation? The post-turn review
        # injects a literal "[主动记忆触发]" hint block. Treat those as
        # silent no-ops in the demo — they're a separate feature with
        # their own smoke coverage.
        joined = " ".join((m.content or "") for m in messages)
        is_review = "[主动记忆触发]" in joined or "[skill review]" in joined
        if is_review and any(m.role == "tool" for m in messages):
            self.calls.append("(review-fork)")
            return LLMResponse(content="无需更新", model=self.model)
        if is_review:
            self.calls.append("(review-fork)")
            return LLMResponse(
                content="",
                model=self.model,
                tool_calls=[
                    LLMToolCall(
                        id="demo-memory-1",
                        name="memory_manage",
                        arguments=json.dumps(
                            {
                                "action": "remember",
                                "kind": "user_fact",
                                "content": DEMO_MEMORY,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ],
            )

        last_user = ""
        for m in reversed(messages):
            if m.role == "user":
                last_user = m.content or ""
                break
        self.calls.append(last_user[:80])

        # Turn 1 — first travel question, wiki cache miss. ~600-char
        # reply against a domestic LLM endpoint is ~2.5s end to end.
        if ("三日游" in last_user or "三天" in last_user) and "怎么安排" not in last_user:
            await self._simulate(2500)
            return LLMResponse(content=ITINERARY, model=self.model)

        # Turn 3 — capability gap, route to mcp-discovery skill.
        # Shorter reply but the agent reasoning + skill body in prompt
        # makes it the same order of magnitude.
        if "工具" in last_user and ("火车票" in last_user or "订" in last_user):
            await self._simulate(1800)
            return LLMResponse(content=MCP_PROPOSAL, model=self.model)

        # Turn 4 — durable preference, memory intent. Short ack reply.
        if "高铁" in last_user or "默认" in last_user or "以后" in last_user:
            await self._simulate(900)
            return LLMResponse(content=MEMORY_ACK, model=self.model)

        # Fallback (should not be hit in this demo)
        return LLMResponse(
            content=f"(scripted) acknowledged: {last_user[:60]}",
            model=self.model,
        )


# =============================================================================
# Pretty printing helpers
# =============================================================================

WIDTH = 72
_RULE = "═" * WIDTH


def banner(title: str) -> None:
    print()
    print(_RULE)
    pad = max(0, (WIDTH - len(title)) // 2)
    print(" " * pad + title)
    print(_RULE)


def section_break() -> None:
    print()
    print("─" * WIDTH)
    print()


def turn_header(idx: int, total: int, user_text: str, hints: list[str]) -> None:
    print()
    print(f"┌── Turn {idx}/{total} " + "─" * (WIDTH - 14))
    print(f"│ 用户: {user_text}")
    for hint in hints:
        print(f"│   ↳ {hint}")
    print("│")


def turn_body(reply_text: str) -> None:
    for line in reply_text.splitlines() or [""]:
        print(f"│  {line}")
    print("│")


def turn_footer(elapsed_ms: float, note: str) -> None:
    print(f"│  ⏱  {elapsed_ms:7.1f} ms — {note}")
    print("└" + "─" * (WIDTH - 1))


def summary_table(rows: list[tuple[str, str, str]]) -> None:
    section_break()
    print("Summary")
    print()
    print(f"  {'Turn':<6} {'Latency':<12} {'Differentiator'}")
    print(f"  {'─' * 4:<6} {'─' * 9:<12} {'─' * 40}")
    for turn, latency, label in rows:
        print(f"  {turn:<6} {latency:<12} {label}")
    print()


# =============================================================================
# Demo execution
# =============================================================================


def _make_msg(seq: int, text: str) -> IncomingMessage:
    return IncomingMessage(
        platform="demo",
        channel_id="demo",
        user_id="demo-user",
        message_id=f"demo-{seq}",
        text=text,
        timestamp=datetime.now(timezone.utc),
        reply_target=DeliveryTarget(
            platform="demo", target_type="user", target_id="demo-user",
        ),
    )


def _run_turn(loop, agent, msg) -> tuple[str, float, int]:
    """Run one turn, return (reply_text, elapsed_ms, llm_calls_this_turn)."""
    pre_calls = len(agent._llm.calls)
    t0 = time.perf_counter()
    reply = loop.run_until_complete(agent.run_turn(msg))
    elapsed_ms = (time.perf_counter() - t0) * 1000
    post_calls = len(agent._llm.calls)
    return (
        reply.text if reply is not None else "(no reply)",
        elapsed_ms,
        post_calls - pre_calls,
    )


def main() -> int:
    banner("ZLAgent · Self-improving IM Agent · End-to-End Demo")
    print()
    print("  Three differentiators in 4 turns, all offline:")
    print("    ① wiki answer cache (v0.37) — first time slow, repeats instant")
    print("    ② skill auto-router (v0.36) — picks the right playbook")
    print("    ③ proactive memory (v0.27) — durable preferences without /commands")
    print()

    summary_rows: list[tuple[str, str, str]] = []

    with TestClient(app) as client:
        agent = client.app.state.agent
        wiki = client.app.state.wiki_store
        memory_store = client.app.state.memory_store

        # Wipe any prior travel-guide cache so turn 1 is a real miss.
        for row in wiki.list_entries(skill_id="travel-guide", limit=50):
            wiki.delete(row["id"])
        for row in memory_store.search(DEMO_MEMORY, limit=50):
            if row.get("content") == DEMO_MEMORY:
                memory_store.remove(row["id"])

        scripted = ScriptedLLM()
        original_llm = agent._llm
        agent._llm = scripted  # type: ignore[assignment]

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # ── Turn 1 — cache MISS, slow path ────────────────────────────
            t1_text = "帮我规划一下北京三日游"
            turn_header(
                1, 4, t1_text,
                ["skill router → travel-guide (matched '三日游')",
                 "wiki cache → MISS — first time"],
            )
            reply, ms, calls = _run_turn(loop, agent, _make_msg(1, t1_text))
            turn_body(reply)
            turn_footer(
                ms, f"LLM calls: {calls} · cached for 30 days on next miss",
            )
            summary_rows.append(
                ("1", f"{ms:.1f} ms", "wiki MISS → LLM (slow path)"),
            )

            # The slow-path write-back is a fire-and-forget asyncio.Task
            # bound to this event loop. Between turns the loop is idle,
            # so the task can't make progress on its own. We have to
            # actively drain the pending-writes set by awaiting the tasks
            # — this is what the loop would do naturally if turns were
            # back-to-back coroutine awaits, but our demo serialises them
            # via run_until_complete so we have to pump the loop here.
            async def _drain_pending_writes() -> None:
                pending = list(getattr(agent, "_pending_wiki_writes", set()))
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            loop.run_until_complete(_drain_pending_writes())
            assert wiki.lookup("travel-guide", "北京三日游") is not None, (
                "write-back failed to land — turn 2 will not hit the cache"
            )

            # ── Turn 2 — cache HIT, fast path (paraphrase) ────────────────
            t2_text = "北京三日游怎么安排"
            turn_header(
                2, 4, t2_text,
                ["skill router → travel-guide (matched '三日游')",
                 "wiki cache → HIT  (normalized key collapses to '北京3日')"],
            )
            reply, ms, calls = _run_turn(loop, agent, _make_msg(2, t2_text))
            turn_body(reply)
            turn_footer(
                ms,
                f"LLM calls: {calls} (zero!) · same answer in ~{ms:.0f}ms",
            )
            summary_rows.append(
                ("2", f"{ms:.1f} ms", "wiki HIT → fast path, NO LLM call"),
            )

            # ── Turn 3 — capability gap, mcp-discovery skill ─────────────
            t3_text = "我需要一个工具帮我订火车票"
            turn_header(
                3, 4, t3_text,
                ["skill router → mcp-discovery (matched '我需要一个工具')",
                 "agent reasons: no existing tool covers ticket booking"],
            )
            reply, ms, calls = _run_turn(loop, agent, _make_msg(3, t3_text))
            turn_body(reply)
            turn_footer(
                ms,
                "agent surfaces 3 install candidates; "
                "operator picks → mcp_manage(add) (gated by confirm)",
            )
            summary_rows.append(
                ("3", f"{ms:.1f} ms", "MCP discovery → install proposal"),
            )

            # ── Turn 4 — durable preference, memory intent ───────────────
            t4_text = "我以后默认坐高铁不坐飞机"
            intent = detect_memory_intent(t4_text)
            intent_label = (
                f"detect_memory_intent → triggered={intent.triggered}, "
                f"confidence={intent.confidence}, "
                f"matched=[{','.join(intent.matched) or '-'}]"
            )
            turn_header(
                4, 4, t4_text,
                [intent_label,
                 "review fork → memory_manage(remember) writes user_fact"],
            )
            reply, ms, calls = _run_turn(loop, agent, _make_msg(4, t4_text))

            async def _drain_pending_reviews() -> None:
                pending = list(getattr(agent, "_pending_reviews", set()))
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            loop.run_until_complete(_drain_pending_reviews())
            stored = [
                row
                for row in memory_store.search(DEMO_MEMORY, limit=20)
                if row.get("content") == DEMO_MEMORY
            ]
            assert stored, "memory review ran but did not persist the preference"
            turn_body(reply)
            turn_footer(
                ms,
                "next time you ask about travel, agent honours 高铁 preference",
            )
            summary_rows.append(
                ("4", f"{ms:.1f} ms", "memory intent → background remember"),
            )

            # ── Wrap-up ──────────────────────────────────────────────────
            summary_table(summary_rows)

            t1_ms = float(summary_rows[0][1].split()[0])
            t2_ms = float(summary_rows[1][1].split()[0])
            ratio = t1_ms / t2_ms if t2_ms > 0 else float("inf")
            print(f"  Speedup turn 1 → turn 2: {ratio:.1f}×")
            print()
            print("  All 4 turns ran offline against the real ZLAgent stack.")
            print("  Only the LLM was faked; cache / router / memory are real.")
            print()
        finally:
            # Drain background tasks before closing the loop. Two
            # populations matter:
            #   - _pending_wiki_writes: drained mid-run already, but
            #     turns 3/4 may have queued more.
            #   - _pending_reviews: turn 4's memory_intent fired the
            #     review fork via loop.create_task. If we close the
            #     loop while it's still suspended, Python GC eventually
            #     runs the coroutine's finally block in a torn-down
            #     context, raising ``Token was created in a different
            #     Context`` when ``reset_current_write_origin`` runs.
            # Awaiting them here lets each task finish naturally in its
            # own task-context, so the ContextVar reset matches the
            # set and the demo shuts down cleanly.
            async def _drain_all() -> None:
                pending = list(getattr(agent, "_pending_wiki_writes", set())) \
                    + list(getattr(agent, "_pending_reviews", set()))
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            try:
                loop.run_until_complete(_drain_all())
            except Exception:  # noqa: BLE001
                pass
            agent._llm = original_llm  # type: ignore[assignment]
            for row in wiki.list_entries(skill_id="travel-guide", limit=50):
                wiki.delete(row["id"])
            for row in memory_store.search(DEMO_MEMORY, limit=50):
                if row.get("content") == DEMO_MEMORY:
                    memory_store.remove(row["id"])
            loop.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
