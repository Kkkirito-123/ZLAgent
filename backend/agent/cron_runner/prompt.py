from __future__ import annotations

from typing import Optional

from loguru import logger

from ..prompts import SKILL_PROMPT_HEADER, SYSTEM_PROMPT_CRON


def compose_cron_user_message(instruction: str, pre_script_output: Optional[str]) -> str:
    body = (instruction or "").strip()
    prefix = (pre_script_output or "").strip()
    if not prefix:
        return body
    return (
        "\u4ee5\u4e0b\u662f pre_script \u5728\u672c\u5468\u671f\u91c7\u96c6\u5230\u7684\u5b9e\u65f6\u6570\u636e\uff0c\u8bf7\u628a\u5b83\u5f53\u4f5c\u6743\u5a01\u4e8b\u5b9e\uff0c"
        "\u4e0d\u8981\u51ed\u7a7a\u7f16\u9020\u6216\u5ffd\u7565\u5176\u4e2d\u7684\u4fe1\u606f\uff1a\n\n```\n"
        f"{prefix}\n```\n\n\u57fa\u4e8e\u4e0a\u9762\u7684\u6570\u636e\uff0c\u6267\u884c\u4e0b\u9762\u7684\u4efb\u52a1\uff1a\n\n{body}"
    )


def build_cron_system_prompt(skill_hint: Optional[str], skill_loader=None) -> str:
    if not skill_hint or skill_loader is None:
        return SYSTEM_PROMPT_CRON
    skill_loader.load()
    body = skill_loader.read_body(skill_hint)
    if not body:
        logger.warning("cron skill_hint={!r} not found or empty; running without skill", skill_hint)
        return SYSTEM_PROMPT_CRON
    manifest = skill_loader.get(skill_hint)
    skill_label = manifest.name if manifest else skill_hint
    return (
        f"{SYSTEM_PROMPT_CRON}\n\n"
        f"--- {SKILL_PROMPT_HEADER} ---\n"
        f"# \u6280\u80fd: {skill_label}\n"
        f"{body}\n"
        f"--- \u6280\u80fd\u6b63\u6587\u7ed3\u675f ---"
    )
