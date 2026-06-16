from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MemoryIntentSignal:
    triggered: bool
    confidence: str = "none"
    reason: str = ""
    matched: tuple[str, ...] = ()


# v0.42 — broadened the trigger surface so the LLM is reminded to
# call ``memory_manage(remember)`` for far more natural phrasings,
# not only the literal "记住" / "remember" form. Inspired by
# Hermes Agent's "do this proactively, don't wait to be asked" doctrine
# — the agent should never make the user repeat themselves twice.
#
# Three buckets:
#   * NEGATIVE — explicit "don't store this" — short-circuit return.
#   * HIGH     — almost certainly worth a memory write (identity
#                corrections, future-default rules, hard nos).
#   * MEDIUM   — probably worth a write; LLM still has the final say.
#
# Each pattern carries a category name that the review hint surfaces
# back to the LLM, so the LLM knows *which kind* of trigger fired.
_NEGATIVE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # 中文 "不用 / 不要 / 别 ... 记"
        r"不用记(住|下来)?",
        r"不要记(住|下来)?",
        r"别记(住|下来)?",
        r"这次不用(记|存)",
        r"临时的?(就行|就好|可以)?",
        r"一次性",
        r"不用长期(记|存)",
        # English explicit don't
        r"don'?t\s+remember",
        r"do\s+not\s+remember",
        r"forget\s+(that|this)",
        r"just\s+for\s+now",
        r"this\s+time\s+only",
    )
)

_HIGH_PATTERNS: dict[str, re.Pattern[str]] = {
    # 显式说"记住"
    "explicit_remember": re.compile(
        r"记住|记下来?|帮我记|存一下|留个备忘|"
        r"remember\s+(this|that|me)|note\s+this\s+down|save\s+this\s+to\s+memory",
        re.IGNORECASE,
    ),
    # "以后 / 默认 / 每次" — 规则性语句
    "future_default": re.compile(
        r"以后|之后(都|起)|从现在开始|从此|从今天起|默认|每次|下次(再|开始|起)|"
        r"一直(用|都)|"
        r"from\s+now\s+on|going\s+forward|in\s+the\s+future|always|"
        r"next\s+time|by\s+default",
        re.IGNORECASE,
    ),
    # "别再 / 不要再" — 硬性禁令
    "stop_doing": re.compile(
        r"别再|不要再|以后不要|以后别|别总是|不要总是|不要(继续)?这样|"
        r"stop\s+(doing|saying|calling)|never\s+(do|say|call)|"
        r"don'?t\s+(do\s+that|say\s+that|call\s+me)",
        re.IGNORECASE,
    ),
    # 身份纠正 — "我叫 X / 我是 X / 别叫我 X" 几乎一定要存。
    # 英文这边只收"my name is" / "call me X" / "don't call me" 这种
    # 没有歧义的形式 —— 故意不写 "i'm X"，因为它会和 "i'm in PST" /
    # "i'm working on …" 这种环境/动作描述大量误中；后者由
    # ``time_locale`` / ``project_context`` 等专用类别命中。
    "identity_correction": re.compile(
        r"我(其实)?(就|不|真的)?叫\s*[\w\u4e00-\u9fff]+|"
        r"我的名字(是|叫)|"
        r"别叫我|不要叫我|不(是|叫)\s*[\w\u4e00-\u9fff]+\s*(吗|啊|嘛)?$|"
        r"my\s+name\s+is\b|"
        r"call\s+me\s+[A-Z]\w*|"
        r"don'?t\s+call\s+me",
        re.IGNORECASE,
    ),
    # "改成 / 换成" 既有信息 — 纠正/更新型
    "value_correction": re.compile(
        r"(改|换)成|应该是\s*[\w\u4e00-\u9fff]+|"
        r"不是.+(是|叫)|"
        r"actually\s+it'?s|it\s+should\s+be|the\s+correct\s+one\s+is",
        re.IGNORECASE,
    ),
}

_MEDIUM_PATTERNS: dict[str, re.Pattern[str]] = {
    # 偏好 — 喜欢 / 习惯 / 倾向
    "preference": re.compile(
        r"我(更|很|挺|特别)?(喜欢|不喜欢|讨厌|偏好|习惯|倾向于?|想要|希望|打算)|"
        r"我的习惯(是|就是)|"
        r"i\s+(like|love|hate|prefer|enjoy|dislike|don'?t\s+like|want\s+to|"
        r"need\s+to|tend\s+to)\b",
        re.IGNORECASE,
    ),
    # agent 应该 / 不应该
    "assistant_should": re.compile(
        r"你应该|你要|你不用|你不要|你别|请你|麻烦你|"
        r"请优先|优先|尽量|尽可能|"
        r"please\s+(do|don'?t|always|never|avoid|prefer)|"
        r"can\s+you\s+(always|never)|could\s+you\s+(always|never)",
        re.IGNORECASE,
    ),
    # 回复风格 / 语气 / 格式
    "style_format": re.compile(
        r"回复.*(简短|详细|短一点|长一点|格式|语气|风格|自然|口语|专业|严谨)|"
        r"格式.*(固定|保持|统一)|语气.*(正式|随意|轻松)|"
        r"太(长|短|啰嗦|正式|生硬)|"
        r"(简单|直接|短)一点|"
        r"reply\s+(shorter|longer|in\s+\w+)|"
        r"(less|more)\s+(verbose|formal|casual)",
        re.IGNORECASE,
    ),
    # 关系 — 家人 / 朋友 / 宠物
    "relationship": re.compile(
        # 直接形式: 我老婆 / 我的猫 / 我儿子
        r"我(的)?(老婆|老公|对象|男朋友|女朋友|配偶|爱人|"
        r"父亲|母亲|爸爸?|妈妈?|"
        r"姐姐|妹妹|哥哥|弟弟|兄弟|姐妹|"
        r"孩子|儿子|女儿|"
        r"宠物|猫|狗|乌龟|鸟|仓鼠|"
        r"室友|同事|领导|老板|同学)|"
        # 我家... 形式: "我家有只猫" / "我家妈妈" (允许 0-4 个连接字符)
        r"我家(里|有|养)?\S{0,4}(老婆|老公|爸爸?|妈妈?|"
        r"父亲|母亲|姐姐|妹妹|哥哥|弟弟|"
        r"孩子|儿子|女儿|"
        r"宠物|猫|狗|乌龟|鸟|仓鼠)|"
        # English
        r"my\s+(wife|husband|partner|gf|bf|girlfriend|boyfriend|spouse|"
        r"dad|mom|mother|father|son|daughter|kid|child|"
        r"sister|brother|sibling|"
        r"pet|cat|dog|"
        r"roommate|coworker|boss|colleague|classmate)",
        re.IGNORECASE,
    ),
    # 重复事件 / 时间表
    "recurring_event": re.compile(
        r"每(天|日|周|月|年|次|小时|半小时|两天|两周)|每隔\s*\d+|"
        r"周[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5\u5929]\s*(都|要)?|"
        r"工作日|休息日|周末|"
        r"早上\s*\d+\s*点|晚上\s*\d+\s*点|"
        r"every\s+(day|week|month|year|morning|night|monday|tuesday|wednesday|"
        r"thursday|friday|saturday|sunday)|"
        r"daily|weekly|monthly|yearly|"
        r"on\s+(weekdays|weekends)",
        re.IGNORECASE,
    ),
    # 时区 / 居住地。中文 "我在" 同时能当动词 ("我在做..."), 所以
    # 只收歧义低的形式: 时区关键词 / "我住在" / "我位于" / "我家在".
    "time_locale": re.compile(
        r"时区|北京时间|上海时间|东八区|UTC[+\-]\d+|"
        r"我(住在|位于)\s*[\w\u4e00-\u9fff]+|"
        r"我家在\s*[\w\u4e00-\u9fff]+|"
        r"timezone|i'?m\s+in\s+\w+|i\s+live\s+in",
        re.IGNORECASE,
    ),
    # 项目 / 工作背景。中间的修饰字 (e.g. "做一个 AI 助手项目") 可能有
    # 空格或字母数字混排，所以用 ``[\w\u4e00-\u9fff\s]{1,20}?`` 桥接。
    "project_context": re.compile(
        r"我(在|正在)?做(一个|个)?[\w\u4e00-\u9fff\s]{1,20}?(项目|应用|网站|工具|"
        r"app|系统|产品|服务|公司|创业)|"
        r"我的项目|我的代码库|我的(repo|仓库)|"
        r"i'?m\s+(working\s+on|building|developing)|"
        r"my\s+(project|repo|codebase|company|startup)",
        re.IGNORECASE,
    ),
    # 工具 / 环境偏好
    "tool_pref": re.compile(
        r"我用\s*[\w\u4e00-\u9fff]+|我习惯用|我的(电脑|机器|手机|系统|环境)|"
        r"i\s+use\s+\w+|my\s+(machine|laptop|phone|system|setup|environment)",
        re.IGNORECASE,
    ),
}


def detect_memory_intent(text: str) -> MemoryIntentSignal:
    content = (text or "").strip()
    if not content:
        return MemoryIntentSignal(triggered=False)
    for pattern in _NEGATIVE_PATTERNS:
        if pattern.search(content):
            return MemoryIntentSignal(
                triggered=False,
                confidence="none",
                reason="negative_memory_request",
                matched=(pattern.pattern,),
            )

    high = [name for name, pattern in _HIGH_PATTERNS.items() if pattern.search(content)]
    if high:
        return MemoryIntentSignal(
            triggered=True,
            confidence="high",
            reason="durable_instruction_or_preference",
            matched=tuple(high),
        )

    medium = [name for name, pattern in _MEDIUM_PATTERNS.items() if pattern.search(content)]
    if medium:
        return MemoryIntentSignal(
            triggered=True,
            confidence="medium",
            reason="possible_user_preference",
            matched=tuple(medium),
        )

    return MemoryIntentSignal(triggered=False)


def render_memory_intent_review_hint(signal: MemoryIntentSignal) -> str:
    """Render a short LLM-facing hint describing which trigger fired.

    The hint nudges the LLM toward calling ``memory_manage(remember)``
    proactively. Tone matches Hermes Agent's "don't wait to be asked"
    doctrine — the user shouldn't have to say a thing twice.
    """
    if not signal.triggered:
        return ""
    matched = ", ".join(signal.matched) if signal.matched else signal.reason
    return (
        "\n\n[主动记忆触发] 本轮命中长期偏好/纠正信号。"
        f" confidence={signal.confidence}; matched={matched}.\n"
        "请直接判断是否调用 memory_manage(remember) 写入 user_fact —— "
        "用户**不应该重复说同一件事第二次**。\n"
        "  - 身份纠正 (identity_correction / value_correction): 几乎一定写。\n"
        "  - 长期规则 (future_default / stop_doing / explicit_remember): 写。\n"
        "  - 偏好/关系/时区/项目背景: 写，除非已被现有 memory 覆盖。\n"
        "如果只是一次性细节、或现有记忆已覆盖，回复「无需更新」。"
    )
