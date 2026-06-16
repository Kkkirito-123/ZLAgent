"""Realtime travel triggers used by :mod:`backend.agent.loop`.

Extracted in v0.40 to keep the AgentLoop module focused on
orchestration. These helpers are pure functions over the raw user
text — no dependency on AgentLoop state — so they live as
module-level callables and are re-exported from ``loop.py`` for
backward compatibility with smoke tests that import them directly.

The triggers split into three complementary mechanisms:

* ``_REALTIME_TRAVEL_MARKERS`` — substring tokens like ``火车票`` /
  ``天气`` that unambiguously call for live data, regardless of
  whether the user named cities.
* ``_REALTIME_TRAVEL_ROUTE_RE`` — a city-pair pattern like
  ``北京到上海`` / ``上海→北京``. We deliberately do NOT include
  ``去`` as a connective because it is too overloaded
  (``帮我去查询`` / ``我们去吃饭``) and would create false
  positives that confuse the routing.
* ``_NON_TRAVEL_BLOCKERS`` — a negative-context list. The route
  regex is permissive enough to match ``加入到我的知识库`` or
  ``保存到笔记`` as if they were city pairs. When any blocker
  appears anywhere in the text we refuse the route-pattern path
  (markers still fire on their own — they're far more specific).
"""
from __future__ import annotations

import re

# marker list trimmed. The old set included ``\u5b9e\u65f6``
# (realtime), ``\u6e29\u5ea6`` (temperature), ``\u5bfc\u822a``
# (navigation), ``\u5730\u56fe`` (map) and ``\u9644\u8fd1`` (nearby),
# all of which routinely appear in non-travel contexts (``CPU
# \u6e29\u5ea6``, ``\u4ee3\u7801\u5bfc\u822a``, ``\u5b9e\u65f6\u540c
# \u6b65``, ``\u601d\u7ef4\u5730\u56fe``, ``\u9644\u8fd1\u7684\u4ee3
# \u7801``). Substring matching them caused the deterministic prefetch
# path to short-circuit non-travel turns and dispatch a misleading
# ``\ud83d\udd0d \u6b63\u5728\u67e5\u8be2\u5b9e\u65f6\u65c5\u884c\u6570
# \u636e`` hint -- see issue logged in v0.40.7. Travel queries that
# legitimately use those words also carry stronger signals (city names
# via the route regex, or specific markers like ``12306`` / ``\u706b
# \u8f66\u7968`` / ``\u822a\u73ed``), so removing the weak tokens from
# the deterministic path has no false-negative impact in practice;
# the LLM can still load the travel skill body via substring trigger.
_REALTIME_TRAVEL_MARKERS = (
    "火车票",
    "高铁",
    "动车",
    "余票",
    "票价",
    "12306",
    "车次",
    "航班",
    "机票",
    "酒店价格",
    "今晚酒店",
    "天气",
    "气温",
    "下雨",
    "降雨",
    "风速",
    "限行",
)


# route patterns alone (e.g. "北京到上海", "上海→北京", "从上海到北京")
# imply the user wants real travel data, not a generic guide. We treat any
# city-to-city pattern with the directional connectives 到 / → / -> / 至 as a
# realtime trigger so the agent calls the travel_realtime tool instead of
# streaming a knowledge-only itinerary.
_REALTIME_TRAVEL_ROUTE_RE = re.compile(
    r"(?:从)?[\u4e00-\u9fa5]{2,8}\s*(?:到|→|->|至)\s*[\u4e00-\u9fa5]{2,8}"
)


# when ANY of these tokens appear we refuse the route-pattern
# path. The regex itself is too liberal: "加入到我的知识库", "保存到笔记",
# "录入到数据库" all match its `X到Y` shape and the agent then runs off
# to 12306 chasing a fictional train to "我的知识库中". The fix is a
# negative-context guard rather than tightening the regex (the latter
# breaks legitimate "上海到北京" / "从北京到上海" forms). The markers
# above (火车票/高铁/12306/天气/…) bypass this guard — they are
# domain-specific enough to fire on their own.
_NON_TRAVEL_BLOCKERS = (
    "知识库",
    "数据库",
    "wiki",
    "笔记",
    "文档",
    "记录",
    "录入",
    "导入",
    "加入到",
    "添加到",
    "归档",
    "保存到",
    "schema",
    "模板",
    "agent",
    "助手",
    "搜索",
    "联网",
)


def _should_route_realtime_travel(text: str) -> bool:
    text_lower = text.lower()
    # Strong domain markers always fire — they are unambiguous live-data asks.
    if any(marker.lower() in text_lower for marker in _REALTIME_TRAVEL_MARKERS):
        return True
    # The route-pattern path is permissive enough to false-positive on
    # non-travel "X到Y" shapes like "加入到我的知识库". Guard it.
    if any(blocker in text_lower for blocker in _NON_TRAVEL_BLOCKERS):
        return False
    if _REALTIME_TRAVEL_ROUTE_RE.search(text):
        return True
    return False


def _build_realtime_loading_hint(text: str) -> str:
    """Compose a short "正在查询..." hint for the realtime travel path so
    the user perceives activity while 12306 / Open-Meteo are running.

    Returns a non-empty string in every branch — even an unrecognised
    realtime query lands on the generic fallback so the user never sees
    a silent dispatch.
    """
    match = _REALTIME_TRAVEL_ROUTE_RE.search(text)
    if match:
        parts = re.split(r"到|→|->|至", match.group(0), maxsplit=1)
        if len(parts) == 2:
            from_city = parts[0].replace("从", "").strip()
            to_city = parts[1].strip()
            if from_city and to_city:
                return (
                    f"🔍 正在查询 {from_city}→{to_city} 的实时火车票 + 当地天气，请稍候…"
                )
    text_lower = text.lower()
    if any(m in text_lower for m in ("火车票", "高铁", "动车", "车次", "余票", "票价", "12306")):
        return "🔍 正在查询 12306 实时余票，请稍候…"
    if any(m in text_lower for m in ("天气", "气温", "温度", "下雨", "降雨", "风速")):
        return "🔍 正在查询实时天气数据，请稍候…"
    return "🔍 正在为你查询实时旅行数据，请稍候…"
