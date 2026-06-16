from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

from ...llm import LLMMessage
from ...llm.prompt_cache import assemble_system_prompt
from ..prompts import SILENT_MARKER
from .prompt import build_cron_system_prompt, compose_cron_user_message

if TYPE_CHECKING:
    from ...memory.manager import MemoryManager
    from ...skills.loader import SkillLoader
    from ..recovery import FailureLearner
    from ..post_turn import PostTurnPipeline


class CronRunner:
    """Orchestrate cron runs through the shared LLM/tool loop."""

    def __init__(
        self,
        *,
        memory: Optional["MemoryManager"] = None,
        failure_learner: Optional["FailureLearner"] = None,
        skill_loader: Optional["SkillLoader"] = None,
        post_turn: "PostTurnPipeline",
        run_tool_loop_fn,
        sync_memory_fn,
    ) -> None:
        self._memory = memory
        self._failure_learner = failure_learner
        self._skill_loader = skill_loader
        self._post_turn = post_turn
        self._run_tool_loop_fn = run_tool_loop_fn
        self._sync_memory_fn = sync_memory_fn

    async def generate(
        self,
        instruction: str,
        *,
        job_name: str,
        skill_hint: Optional[str] = None,
        pre_script_output: Optional[str] = None,
        llm_configured: bool,
        llm: Any,
    ) -> str:
        text = (instruction or "").strip()
        if not text and not pre_script_output:
            return f"定时任务「{job_name}」到点，但这份作业没有作业指令。"
        if not llm_configured:
            return f"{pre_script_output.strip()}\n\n{text}".strip() if pre_script_output else text

        cron_stable_prompt = build_cron_system_prompt(skill_hint, self._skill_loader)
        user_message = compose_cron_user_message(text, pre_script_output)

        cron_dynamic_sections: list[str] = []
        if self._memory is not None:
            self._memory.on_turn_start(
                user_message,
                platform="cron",
                user_id=job_name,
                interactive=False,
                session_id=f"cron:{job_name}",
            )
            snapshot = self._memory.system_prompt_block()
            if snapshot:
                cron_dynamic_sections.append(snapshot)

        system_prompt = assemble_system_prompt(cron_stable_prompt, "\n\n".join(s for s in cron_dynamic_sections if s))
        history: list[LLMMessage] = [LLMMessage(role="system", content=system_prompt)]
        if self._memory is not None and text:
            prefetched = self._memory.prefetch(text)
            for block in (
                self._memory.render_prefetch_block(prefetched),
                self._memory.provider_prefetch_block(text, session_id=f"cron:{job_name}"),
            ):
                if block:
                    history.append(LLMMessage(role="system", content=block))
        history.append(LLMMessage(role="user", content=user_message))

        outcome = await self._run_tool_loop_fn(
            history,
            system=system_prompt,
            interactive=False,
            platform="",
            user_id="",
            reply_target=None,
            user_text_for_fallback=text,
            session_id=f"cron:{job_name}",
        )
        if (
            self._failure_learner is not None
            and outcome.suspended_confirmation_id is None
            and outcome.tool_outcomes
        ):
            self._failure_learner.observe_turn(outcome.tool_outcomes)
        self._post_turn._schedule_review(
            outcome=outcome,
            history=history,
            platform="cron",
            user_id=job_name,
            llm=llm,
        )
        result = (outcome.final_text or text).strip()
        self._sync_memory_fn(
            user_content=user_message,
            assistant_content=result,
            session_id=f"cron:{job_name}",
            metadata={
                "platform": "cron",
                "job_name": job_name,
                "interactive": False,
                "skill_hint": skill_hint or "",
                "tool_outcomes": outcome.tool_outcomes,
                "invoked_tools": outcome.invoked_tool_names,
            },
            outcome=outcome,
        )
        if result.upper() == SILENT_MARKER or result.upper().startswith(SILENT_MARKER + "\n"):
            return SILENT_MARKER
        return result
