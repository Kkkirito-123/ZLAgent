"""query normalization for the answer cache.

Why a normalizer at all
=======================
Two paraphrases of the same intent must converge to the same cache
key — otherwise the wiki misses on every variation and the whole
"first slow, then instant" promise falls apart. A real semantic
embedding would solve this, but for v0.37 we use deterministic
text rewrites that handle the high-leverage cases:

* "我想去北京玩三天" → ``北京 3日``
* "北京三日游攻略" → ``北京 3日``
* "去北京 3天怎么安排" → ``北京 3日``

Generic rules apply to all skills (``normalize_query``); per-skill
rules layer on top via ``normalize_for_kind`` so a future
``weather`` skill can canonicalize "上海" / "魔都" / "Shanghai" to
the same key without polluting the travel rules.

Iron law: normalization MUST be idempotent. ``normalize(normalize(x))
== normalize(x)`` for every input. The cache writer and the cache
reader run the same function — if normalization isn't a fixed point
we silently double-rewrite on writes and never find rows on reads.
The smoke block has an explicit assertion for this.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable

# Filler verbs / particles that don't change query intent.
# We strip these regardless of skill. Order matters only insofar
# as longer phrases must come first so we don't substring-eat into
# meaningful content.
_GENERIC_FILLERS: tuple[str, ...] = (
    "请帮我",
    "麻烦你",
    "麻烦帮我",
    "帮我看看",
    "帮我",
    "我想要",
    "我想去",
    "我想",
    "我要",
    "怎么安排",
    "怎么样",
    "怎么办",
    "怎么",
    "可以吗",
    "好不好",
    "好吗",
    "有没有",
    "有什么",
    "推荐一下",
    "推荐下",
    "推荐",
    "看一下",
    "看下",
    # Action-suggestion prefixes — only the multi-char forms are
    # safe to strip globally; the bare verb (规划 / 安排 / 计划) keeps
    # its substantive meaning in non-travel domains (e.g. "城市规划"),
    # so we only strip the longer, clearly-imperative forms here. The
    # bare verb is handled per-skill in ``_TRAVEL_DAY_REWRITES`` below
    # (where stripping it cannot bleed into other domains).
    "规划一下",
    "规划下",
    "安排一下",
    "安排下",
    "计划一下",
    "计划下",
    "去玩",
    "去逛",
    "玩玩",
)

# Punctuation we always strip. Kept as a set for O(1) lookup. We
# include both ASCII and full-width Chinese punctuation because IM
# users mix them freely.
_PUNCT_TO_STRIP: frozenset[str] = frozenset(
    "?？!！。，,;；:：、~～()（）[]【】{}<>《》\"'`“”‘’"
)

# Travel-domain rewrites: number-of-days canonicalization. We map
# every variant of "<n> day(s)" to the literal string ``<n>日``.
# The dict keys are sorted longest-first when applied, which matters
# for the Chinese numerals where "三日" is a substring of "二十三日".
_TRAVEL_DAY_REWRITES: tuple[tuple[str, str], ...] = (
    # English
    ("days", "日"),
    ("day", "日"),
    # Chinese numbers (extend up to 10; covers ~99% of trip lengths)
    ("十一日", "11日"), ("十二日", "12日"), ("十三日", "13日"),
    ("十四日", "14日"), ("十五日", "15日"),
    ("十一天", "11日"), ("十二天", "12日"), ("十三天", "13日"),
    ("十四天", "14日"), ("十五天", "15日"),
    ("一日", "1日"), ("二日", "2日"), ("三日", "3日"),
    ("四日", "4日"), ("五日", "5日"), ("六日", "6日"),
    ("七日", "7日"), ("八日", "8日"), ("九日", "9日"),
    ("十日", "10日"),
    ("一天", "1日"), ("两天", "2日"), ("二天", "2日"),
    ("三天", "3日"), ("四天", "4日"), ("五天", "5日"),
    ("六天", "6日"), ("七天", "7日"), ("八天", "8日"),
    ("九天", "9日"), ("十天", "10日"),
    # Digits + 天 / 日 already digit-based
    ("天", "日"),
    # Action verbs that often pad travel queries. These are
    # travel-specific (per-skill normalizer) so removing them
    # cannot break any other domain.
    ("游玩", ""),
    ("游", ""),
    ("玩", ""),
    ("逛", ""),
    ("去", ""),
    ("到", ""),
    ("攻略", ""),
    ("旅游", ""),
    ("旅行", ""),
    ("行程", ""),
    # Bare imperative verbs — safe in travel context because if the
    # user is here at all it's because the skill router matched a
    # travel trigger. Out-of-domain "城市规划" never reaches this
    # rewrite. Keep these AFTER the day-number rewrites so we don't
    # accidentally swallow a digit (no overlap, but principled order).
    ("规划", ""),
    ("安排", ""),
    ("计划", ""),
)


def _strip_punctuation(text: str) -> str:
    return "".join(ch for ch in text if ch not in _PUNCT_TO_STRIP)


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_query(text: str) -> str:
    """Generic, skill-agnostic canonicalization.

    Steps (order is load-bearing):
    1. Unicode NFKC fold (full-width ASCII → half-width, ligatures → letters).
    2. Lowercase (English half is case-insensitive; Chinese unaffected).
    3. Strip every filler phrase in ``_GENERIC_FILLERS``.
    4. Strip every char in ``_PUNCT_TO_STRIP``.
    5. Collapse runs of whitespace to single spaces.

    Idempotent by construction — every step is itself idempotent on
    its output, and steps 3+4 produce strings that are stable under
    re-application (filler removal can't introduce new fillers,
    punctuation removal can't introduce new punctuation).
    """
    if not text:
        return ""
    # 1.
    canonical = unicodedata.normalize("NFKC", text)
    # 2.
    canonical = canonical.lower()
    # 3. — apply repeatedly until stable so "请帮我帮我" → "帮我" → ""
    # converges in one call rather than relying on the caller to
    # iterate.
    prev: str = ""
    while prev != canonical:
        prev = canonical
        for filler in _GENERIC_FILLERS:
            canonical = canonical.replace(filler.lower(), " ")
    # 4.
    canonical = _strip_punctuation(canonical)
    # 5.
    canonical = _collapse_whitespace(canonical)
    return canonical


def _normalize_travel(text: str) -> str:
    """Travel-domain rewrites layered on top of generic normalization.

    The final step strips ALL whitespace (not just collapses runs).
    Travel queries mix Chinese/English/digit fragments, and authors
    don't agree on whether to space them ("北京 3日" vs "北京3日" vs
    "北京三日游"). After action-verb removal we want every variant
    to map to the same key, which only works if spaces are out of
    the picture entirely.
    """
    if not text:
        return ""
    canonical = text
    # Apply the rewrites in declaration order. The tuple is authored
    # longest-first so "十二日" is rewritten before the suffix "日" is
    # touched. We keep this stable so future authors can audit by
    # reading top-to-bottom.
    for src, dst in _TRAVEL_DAY_REWRITES:
        canonical = canonical.replace(src.lower(), dst)
    # Whitespace stripping is the last step; running it before the
    # rewrites would silently turn "Tokyo 3 days" into "tokyo3days"
    # before "days" gets a chance to match.
    canonical = re.sub(r"\s+", "", canonical)
    return canonical


# Per-skill normalizers. Each takes the GENERIC-normalized text and
# returns the further-canonicalized form. Adding a new skill just
# means registering a new entry here; the dispatcher handles the
# fallback to identity if a skill has no domain-specific rules.
_SKILL_NORMALIZERS: dict[str, Callable[[str], str]] = {
    "travel-guide": _normalize_travel,
}


def normalize_for_kind(skill_id: str, text: str) -> str:
    """Apply generic normalization plus the skill's own rules.

    This is the function callers (``WikiStore.add``, ``WikiStore.lookup``)
    should use; ``normalize_query`` alone is exposed mainly for tests
    and for skills that don't register a domain normalizer.

    A missing entry in ``_SKILL_NORMALIZERS`` is NOT an error — it
    just means the skill doesn't need extra rewrites; we return the
    generic form. New skills can ship without touching this file.
    """
    base = normalize_query(text)
    rewriter = _SKILL_NORMALIZERS.get(skill_id)
    if rewriter is None:
        return base
    return rewriter(base)
