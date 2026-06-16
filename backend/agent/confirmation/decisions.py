"""Yes/no decision classifier for confirmation replies.

Treated as a separate module so the rules are easy to extend (more
languages, fuzzier matching, etc.) without touching the agent loop.

The classifier is intentionally **strict**: it returns ``None`` for anything
ambiguous so the resume path can fall back to a normal agent turn rather
than misinterpret the user's intent. False positives here would silently
execute (or refuse) tool calls the user didn't actually approve.
"""
from __future__ import annotations

import re
from typing import Optional

# Each pattern matches a *whole* user message after stripping & lowercasing.
# Single-word affirmations / negations only — anything longer is ambiguous
# and falls through to the agent loop as a fresh turn.
_YES = re.compile(
    r"^(?:y|yes|ok|okay|sure|approve|approved|allow|go|do it|"
    r"是|好|确认|确定|执行|同意|可以|批准|继续|行)\W*$",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^(?:n|no|nope|cancel|deny|denied|stop|abort|don't|do not|"
    r"否|不|取消|拒绝|算了|不要|停|别)\W*$",
    re.IGNORECASE,
)


def classify_decision(text: str) -> Optional[bool]:
    """Return ``True`` for yes, ``False`` for no, ``None`` for anything else.

    >>> classify_decision("yes")
    True
    >>> classify_decision("是")
    True
    >>> classify_decision("不")
    False
    >>> classify_decision("yes please also do X") is None
    True
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if _YES.match(stripped):
        return True
    if _NO.match(stripped):
        return False
    return None
