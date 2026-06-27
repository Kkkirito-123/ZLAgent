from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Any

_WORD_RE = re.compile(r"[a-zA-Z0-9_\-]+|[\u4e00-\u9fff]")

_QUERY_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "沟通": ("回复", "语气", "风格", "简短", "详细"),
    "风格": ("回复", "语气", "格式", "简短", "详细"),
    "偏好": ("喜欢", "不喜欢", "希望", "习惯"),
    "格式": ("输出", "markdown", "标题", "列表"),
    "称呼": ("叫", "名字", "昵称"),
}


def rank_memories(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not candidates:
        return []
    if not q:
        return candidates[: max(1, limit)]

    q_terms = _expanded_terms(q)
    q_grams = _char_grams(q)
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in candidates:
        content = str(row.get("content") or "")
        c_lower = content.lower()
        q_lower = q.lower()
        score = 0.0
        if q_lower and q_lower in c_lower:
            score += 8.0
        c_terms = _terms(content)
        if q_terms and c_terms:
            overlap = q_terms & c_terms
            score += 4.0 * (len(overlap) / max(1, len(q_terms)))
        c_grams = _char_grams(content)
        if q_grams and c_grams:
            score += 3.0 * (len(q_grams & c_grams) / max(1, len(q_grams)))
        ratio = SequenceMatcher(None, q_lower, c_lower).ratio()
        if ratio >= 0.12:
            score += 2.0 * ratio
        if row.get("pinned"):
            score += 0.8
        score += min(0.5, math.log1p(int(row.get("recall_count") or 0)) / 10)
        if score > 0.05:
            scored.append((score, row))

    scored.sort(
        key=lambda item: (
            item[0],
            bool(item[1].get("pinned")),
            int(item[1].get("recall_count") or 0),
            str(item[1].get("created_at") or ""),
        ),
        reverse=True,
    )
    return [row for _, row in scored[: max(1, limit)]]


def _expanded_terms(text: str) -> set[str]:
    terms = _terms(text)
    expanded = set(terms)
    for term in terms:
        for key, values in _QUERY_EXPANSIONS.items():
            if key in term or term in key:
                expanded.update(values)
    return expanded


def _terms(text: str) -> set[str]:
    raw = [m.group(0).lower() for m in _WORD_RE.finditer(text or "")]
    merged: set[str] = {t for t in raw if len(t) >= 2 or _is_cjk(t)}
    cjk_chars = "".join(t for t in raw if _is_cjk(t))
    for n in (2, 3):
        for i in range(0, max(0, len(cjk_chars) - n + 1)):
            merged.add(cjk_chars[i:i + n])
    return merged


def _char_grams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", (text or "").lower())
    if len(compact) < 2:
        return set()
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def _is_cjk(text: str) -> bool:
    return len(text) == 1 and "\u4e00" <= text <= "\u9fff"
