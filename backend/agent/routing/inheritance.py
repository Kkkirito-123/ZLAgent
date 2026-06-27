from __future__ import annotations

import re

_SKILL_INHERIT_BLOCKLIST_RE = re.compile(
    r"(你是谁|你是什么|是什么助手|什么助手|能干嘛|能干什么|能做什么|"
    r"可以做什么|有什么功能|功能介绍|介绍一下你|自我介绍|help|帮助)"
)
_CONFIRMATION_ONLY_RE = re.compile(
    r"^(?:是|不是|对|不对|好|好的|可以|不可以|行|不行|都行|都可以|"
    r"继续|不用|要|不要|嗯|嗯嗯|收到|ok|okay|确认|同意|没问题)$"
)
# Travel slot filling must be much stricter than generic confirmation.
# Only real trip constraints should carry the prior travel skill forward;
# bare acknowledgements like ``可以`` must NOT inherit travel-guide.
_TRAVEL_SLOT_FILL_RE = re.compile(
    r"^(?:[一二两三四五六七八九十0-9]+(?:天|日)(?:吧|左右|以内)?|"
    r"(?:今天|明天|后天|大后天|周末|这个周末|下周|五一|国庆|春节)(?:吧)?|"
    r"(?:从)?[\u4e00-\u9fa5]{2,8}出发(?:吧)?|"
    r"预算[0-9一二三四五六七八九十百千万kK]+(?:元)?(?:左右|以内)?|"
    r"[0-9一二三四五六七八九十百千万kK]+(?:元|块)(?:左右|以内)?|"
    r"(?:带孩子|带娃|带爸妈|带老人|亲子|情侣|自驾|高铁|火车|飞机|"
    r"轻松点|别太累|不太累|省钱点|便宜点))$"
)
_GENERIC_SLOT_FILL_RE = re.compile(
    r"^(?:是|不是|对|不对|好|好的|可以|不可以|行|不行|都行|都可以|"
    r"继续|不用|要|不要|今天|明天|后天|周末|"
    r"[一二两三四五六七八九十0-9]+(?:天|日|点|分钟|小时)(?:吧)?)$"
)
_TRAVEL_SLOT_FILL_MARKERS = (
    "出发", "预算", "今天", "明天", "后天", "大后天", "周末", "下周",
    "五一", "国庆", "春节", "坐火车", "火车", "高铁", "飞机", "自驾",
    "带孩子", "带娃", "带爸妈", "带老人", "亲子", "情侣", "轻松",
    "别太累", "不太累", "省钱", "便宜",
)


def is_confirmation_only(text: str) -> bool:
    compact = _compact_for_skill_inheritance(text)
    return bool(compact) and _CONFIRMATION_ONLY_RE.match(compact) is not None


def _compact_for_skill_inheritance(text: str) -> str:
    return re.sub(
        r"[\s?？!！。。，,;；:：、~～()（）\[\]【】{}<>《》\"'`“”‘’]",
        "",
        (text or "").lower(),
    )


def should_inherit_skill_hint(text: str, skill_id: str) -> bool:
    compact = _compact_for_skill_inheritance(text)
    if not compact or not skill_id:
        return False
    if _SKILL_INHERIT_BLOCKLIST_RE.search(compact):
        return False
    if skill_id == "travel-guide":
        if _TRAVEL_SLOT_FILL_RE.match(compact):
            return True
        return (
            len(compact) <= 30
            and any(marker in compact for marker in _TRAVEL_SLOT_FILL_MARKERS)
        )
    return len(compact) <= 12 and _GENERIC_SLOT_FILL_RE.match(compact) is not None


_should_inherit_skill_hint = should_inherit_skill_hint
