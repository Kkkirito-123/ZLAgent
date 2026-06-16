"""Confirmation question / direct-reply formatters and the cron humanizer.

Extracted in v0.40 from :mod:`backend.agent.loop`. Every function
in here is a **pure** transformation of plain values to strings —
no AgentLoop state, no I/O, no logging. That made them the easiest
~250 lines to lift out of the god class.

The IM-side flow is:

* ``format_confirmation_question(tool_name, arguments)`` — render
  the yes/no card the user sees when a confirm-tier tool is about
  to fire. Per-tool-family branches (``skill_manage`` / ``cron_manage``
  / ``send_message``) keep the cards readable.
* ``direct_confirmation_reply(tool_name, arguments, decision, result)``
  — render the post-confirm one-liner ("已发送。" / "好了，提醒已更新…").
* ``humanize_cron(...)`` / ``humanize_day_part(...)`` — translate a
  5-field cron expression into a friendly Chinese phrase.
* ``confirmation_preview(value, limit)`` — argument truncation that's
  used by every formatter above (and also by the generic fallback
  card).

``loop.py`` keeps thin ``@staticmethod`` shims on ``AgentLoop`` for
backward compatibility with smoke tests that import them as
``AgentLoop._format_confirmation_question`` / etc.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from croniter import croniter

from ...tools import ToolResult
from ..prompts import SHANGHAI_TZ

# Per-argument display cap inside the confirmation question. The tool itself
# may have a much larger limit (write_file accepts up to 256 KiB), but the
# *question* needs to be readable in an IM bubble.
QUESTION_ARG_PREVIEW_CHARS = 400


def confirmation_preview(value: object, limit: int = QUESTION_ARG_PREVIEW_CHARS) -> str:
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=False)
    else:
        rendered = str(value)
    if len(rendered) > limit:
        rendered = rendered[:limit] + f"... (+{len(rendered) - limit} 字符)"
    return rendered


def format_confirmation_question(tool_name: str, arguments: dict) -> str:
    if tool_name == "skill_manage":
        return format_skill_confirmation(arguments)
    if tool_name == "cron_manage":
        return format_cron_confirmation(arguments)
    if tool_name == "send_message":
        return format_send_message_confirmation(arguments)

    lines = [f"🔧 需要确认执行：{tool_name}"]
    if arguments:
        for key, value in arguments.items():
            rendered = confirmation_preview(value)
            lines.append(f"  {key}: {rendered}")
    else:
        lines.append("  (no arguments)")
    lines.append("")
    lines.append("回复 yes 执行 / no 取消（5 分钟内有效）")
    return "\n".join(lines)


def format_skill_confirmation(arguments: dict) -> str:
    action = str(arguments.get("action") or "").strip() or "操作"
    name = str(arguments.get("skill_name") or "").strip() or "(未命名)"
    description = str(arguments.get("skill_description") or "").strip()
    action_label = {
        "create": "保存一个可复用技能",
        "edit": "替换一个技能",
        "patch": "修改一个技能",
        "write_file": "写入技能附属文件",
        "remove_file": "删除技能附属文件",
        "delete": "删除一个技能",
        "pin": "固定一个技能",
        "unpin": "取消固定一个技能",
        "archive": "归档一个技能",
    }.get(action, f"执行技能操作：{action}")
    lines = [f"🧠 需要确认：{action_label}", f"名称：{name}"]
    if description:
        lines.append(f"用途：{confirmation_preview(description, 120)}")
    file_path = str(arguments.get("file_path") or "").strip()
    if file_path:
        lines.append(f"文件：{file_path}")
    lines.append("")
    lines.append("回复 yes 执行 / no 取消（5 分钟内有效）")
    return "\n".join(lines)


def format_cron_confirmation(arguments: dict) -> str:
    action = str(arguments.get("action") or "").strip() or "操作"
    name = str(arguments.get("name") or "").strip()
    cron_expr = str(arguments.get("cron_expr") or "").strip()
    instruction = str(arguments.get("instruction") or "").strip()
    skill_hint = str(arguments.get("skill_hint") or "").strip()
    lines = [f"⏰ 需要确认：{cron_action_label(action)}"]
    if name:
        lines.append(f"名称：{name}")
    if cron_expr:
        lines.append(f"时间：{cron_expr}（北京时间）")
    if instruction:
        lines.append(f"内容：{confirmation_preview(instruction, 120)}")
    if arguments.get("run_once", False):
        lines.append("类型：一次性提醒")
    if skill_hint:
        lines.append(f"参考技能：{skill_hint}")
    if arguments.get("deliver_to_current_chat", True):
        lines.append("发送到：当前聊天")
    elif arguments.get("delivery_target_id") is not None:
        lines.append(f"发送到：delivery target #{arguments.get('delivery_target_id')}")
    lines.append("")
    lines.append("回复 yes 执行 / no 取消（5 分钟内有效）")
    return "\n".join(lines)


def format_send_message_confirmation(arguments: dict) -> str:
    text = str(arguments.get("text") or "").strip()
    lines = ["📨 需要确认：发送消息"]
    if text:
        lines.append(f"内容：{confirmation_preview(text, 160)}")
    if arguments.get("to_current_chat", True):
        lines.append("发送到：当前聊天")
    elif arguments.get("delivery_target_id") is not None:
        lines.append(f"发送到：delivery target #{arguments.get('delivery_target_id')}")
    lines.append("")
    lines.append("回复 yes 执行 / no 取消（5 分钟内有效）")
    return "\n".join(lines)


def cron_action_label(action: str) -> str:
    return {
        "create": "创建定时任务",
        "update": "更新定时任务",
        "pause": "暂停定时任务",
        "resume": "恢复定时任务",
        "remove": "删除定时任务",
        "run": "立即运行定时任务",
        "list": "查看定时任务",
    }.get(action, f"执行定时任务操作：{action}")


def direct_confirmation_reply(
    tool_name: str,
    arguments: dict,
    decision: bool,
    result: ToolResult,
) -> Optional[str]:
    if not decision:
        return "已取消。"
    if not result.ok:
        return f"执行失败：{result.error or result.content or '未知错误'}"
    if tool_name == "cron_manage":
        return direct_cron_reply(arguments, result)
    if tool_name == "send_message":
        return "已发送。"
    return None


def direct_cron_reply(arguments: dict, result: ToolResult) -> str:
    # IM-friendly one-liner. The verbose internal name + raw cron
    # expression + full LLM-rewritten instruction are useful for the
    # operator REST view, but they read like debug output to a human
    # in chat — see screenshot 2026-05-09. Keep this reply short and
    # human; the cron job rows still carry the full detail.
    action = str(arguments.get("action") or "").strip().lower()
    cron_expr = str(arguments.get("cron_expr") or "").strip()
    run_once = bool(arguments.get("run_once", False))
    if action == "create":
        time_phrase = humanize_cron(cron_expr, run_once=run_once)
        if run_once:
            return f"好的，我会在{time_phrase}提醒你 ⏰"
        return f"好的，已设定提醒：{time_phrase} ⏰"
    if action == "update":
        time_phrase = humanize_cron(cron_expr, run_once=run_once)
        return f"好了，提醒已更新：{time_phrase}"
    if action == "pause":
        return "已暂停。"
    if action == "resume":
        return "已恢复。"
    if action == "remove":
        return "已删除。"
    if action == "run":
        return "已立即执行一次。"
    return result.content or "操作已完成。"


def humanize_cron(cron_expr: str, *, run_once: bool) -> str:
    """Translate a 5-field cron expression into a friendly Chinese phrase.

    The goal is the "for human" tone the user asked for: replace
    ``40 17 9 5 *`` with ``今天下午 17:40`` (or ``5月9日 17:40``),
    and ``0 8 * * *`` with ``每天 08:00``. Falls back to the raw
    expression when we cannot recognise it cleanly so the operator
    still sees something actionable.
    """
    expr = (cron_expr or "").strip()
    if not expr:
        return "(未指定时间)"
    parts = expr.split()
    if len(parts) != 5:
        return f"`{expr}`"
    minute, hour, dom, month, dow = parts

    # One-shot: croniter knows exactly when this fires next, so we
    # render it as a concrete date/time relative to today.
    if run_once:
        try:
            now = datetime.now(SHANGHAI_TZ)
            fire = croniter(expr, now).get_next(datetime).astimezone(SHANGHAI_TZ)
        except (ValueError, KeyError):
            return f"`{expr}`"
        today = now.date()
        tomorrow = today + timedelta(days=1)
        hh_mm = fire.strftime("%H:%M")
        day_part = humanize_day_part(fire.hour)
        if fire.date() == today:
            return f"今天{day_part} {hh_mm}"
        if fire.date() == tomorrow:
            return f"明天{day_part} {hh_mm}"
        return f"{fire.month}月{fire.day}日{day_part} {hh_mm}"

    # Recurring patterns we can cleanly describe. Anything we can't
    # parse falls through to the raw expression.
    try:
        mm = int(minute)
        hh = int(hour)
        time_str = f"{hh:02d}:{mm:02d}"
    except ValueError:
        mm = hh = None
        time_str = ""

    # ``*/N * * * *`` — every N minutes
    if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
        try:
            step = int(minute[2:])
            return f"每 {step} 分钟"
        except ValueError:
            return f"`{expr}`"

    # ``0 */N * * *`` — every N hours on the hour
    if minute == "0" and hour.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        try:
            step = int(hour[2:])
            return f"每 {step} 小时整点"
        except ValueError:
            return f"`{expr}`"

    if mm is None or hh is None:
        return f"`{expr}`"

    # Daily / weekly / weekdays / weekend / monthly variants.
    if dom == "*" and month == "*":
        if dow == "*":
            return f"每天 {time_str}"
        if dow == "1-5":
            return f"工作日 {time_str}"
        if dow in ("0,6", "6,0", "0,7", "6,7"):
            return f"周末 {time_str}"
        try:
            n = int(dow)
            names = {0: "周日", 1: "周一", 2: "周二", 3: "周三",
                     4: "周四", 5: "周五", 6: "周六", 7: "周日"}
            if n in names:
                return f"每{names[n]} {time_str}"
        except ValueError:
            pass
    if month == "*" and dow == "*":
        try:
            d = int(dom)
            return f"每月 {d} 号 {time_str}"
        except ValueError:
            pass
    return f"`{expr}` {time_str}".strip()


def humanize_day_part(hour: int) -> str:
    if 0 <= hour < 5:
        return "凌晨"
    if 5 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 19:
        return "下午"
    return "晚上"
