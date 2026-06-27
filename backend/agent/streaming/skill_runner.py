from __future__ import annotations

import asyncio
from typing import Callable, Optional, TYPE_CHECKING

from loguru import logger

from ...gateways.base import DeliveryTarget, OutgoingMessage
from ...llm import LLMMessage

if TYPE_CHECKING:
    from ...skills.loader import SkillStreamingSection


async def run_streaming_skill(
    *,
    llm,
    manifest,
    safe_text: str,
    session_id: str,
    reply_target: Optional[DeliveryTarget],
    dispatch_fn: Optional[Callable],
    base_history: list[LLMMessage],
) -> str:
    sections: list["SkillStreamingSection"] = manifest.streaming_sections
    sem = asyncio.Semaphore(max(1, manifest.streaming_parallel))
    section_answers: dict[int, str] = {}

    async def _run_one(idx: int, section: "SkillStreamingSection") -> None:
        async with sem:
            section_system = (
                base_history[0].content
                + "\n\n\u4ee5\u4e0b\u662f\u672c\u6b21**\u4ec5\u9700\u8f93\u51fa**\u7684\u5185\u5bb9\u8981\u6c42\uff0c\u5176\u4ed6\u90e8\u5206\u8bf7\u52ff\u8f93\u51fa\uff1a\n"
                + section.prompt
            )
            sec_history = [
                LLMMessage(role="system", content=section_system),
                *base_history[1:],
                LLMMessage(role="user", content=safe_text),
            ]
            try:
                assert llm is not None
                resp = await llm.chat(sec_history, tools=None, tool_choice=None, session_id=session_id)
                answer = (resp.content or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[streaming] section '{}' LLM failed: {}", section.id, exc)
                answer = ""
        section_answers[idx] = answer
        if answer and dispatch_fn is not None:
            prefix = f"{section.title}\n" if section.title else ""
            try:
                await dispatch_fn(OutgoingMessage(target=reply_target, text=f"{prefix}{answer}"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[streaming] dispatch failed section '{}': {}", section.id, exc)

    await asyncio.gather(
        *[asyncio.create_task(_run_one(i, sec)) for i, sec in enumerate(sections)],
        return_exceptions=True,
    )
    parts = [
        (f"{sections[i].title}\n{section_answers[i]}" if sections[i].title else section_answers[i])
        for i in range(len(sections))
        if section_answers.get(i)
    ]
    return "\n\n".join(parts)
