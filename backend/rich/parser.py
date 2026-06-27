"""Extract :class:`RichMessage` from LLM markdown output (v0.38).

A skill that opts into ``rich_output`` instructs the LLM to wrap its
structured payload in a fenced block tagged ``rich-content``; the
surrounding text becomes the fallback (plain-text) representation::

    Looking at your itinerary I'd suggest…

    ```rich-content
    {
      "title": "杭州 3 天行程",
      "blocks": [
        {"kind": "kv", "title": "去程", "pairs": [
          ["车次", "G225"],
          ["时间", "上海虹桥 09:00 → 杭州东 09:45"]
        ]}
      ]
    }
    ```

    所以这趟早上出发完全来得及。

The parser pulls the JSON block out, leaves the rest of the message as
the fallback, and silently degrades on any failure (bad JSON, missing
fields, fence not present) — the original markdown then becomes the
plain-text answer. This "fail open" behaviour matches the agent's
philosophy: never lose the answer to a parsing bug.

Why not arbitrary JSON, why a fence
-----------------------------------

Letting the LLM emit raw JSON forces every model to produce *only*
JSON — that breaks fluent narrative answers and trips on stray
Chinese punctuation inside string fields. A code fence is the most
reliable separator OpenAI / Anthropic / DeepSeek / Qwen models all
respect; the language tag (``rich-content``) is unique enough to not
collide with code samples a tool result might contain.

Multiple fences
---------------

If the LLM emits more than one ``rich-content`` fence we currently
keep the *first* (with a debug log). Future expansion (e.g. multi-card
conversations) can change this; for now it would surprise the user to
see the cards out of authoring order.

Streaming compatibility
-----------------------

The parser only runs on the *complete* assistant message (after the
LLM finishes), so it does not interact with v0.37.9 token streaming.
Skills that opt into rich_output should also disable per-token
streaming for that turn (otherwise the user briefly sees raw JSON
mid-stream). The agent loop wires this gate.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from .schema import RichMessage


_FENCE_RE = re.compile(
    # ``` opening, optional language tag (require rich-content), newline,
    # body (non-greedy), then closing ``` on its own boundary. We don't
    # require the closing to be on its own line (real LLM output
    # sometimes has trailing whitespace) but we do require at least 3
    # backticks to avoid matching inline code spans.
    r"```\s*rich[-_]content\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(slots=True)
class ExtractResult:
    """Outcome of :func:`extract_rich_content`.

    ``rich`` is None when no fence was found OR the fence's JSON
    failed to parse. ``fallback_text`` is what the gateway should
    actually send when ``rich`` is None or the renderer can't handle
    rich payloads — it's the original ``raw_text`` minus the fence
    block, trimmed.

    ``raw_json`` is the literal fence body (post-strip) for diagnostic
    logging when parsing fails. Always a string (empty when no fence).

    ``parse_error`` carries a short diagnostic when JSON parsing
    failed; None on the success path.
    """

    rich: Optional[RichMessage]
    fallback_text: str
    raw_json: str = ""
    parse_error: Optional[str] = None


def extract_rich_content(raw_text: str) -> ExtractResult:
    """Pull the ``rich-content`` fence out of ``raw_text``.

    Behaviour matrix:

    * **No fence** → ``rich=None``, ``fallback_text=raw_text``.
    * **Fence with parseable JSON + rich body** →
      ``rich=RichMessage(...)``,
      ``fallback_text``= raw text with fence removed.
      The RichMessage's ``fallback_text`` slot is also populated
      (with the same trimmed text) so downstream consumers can use
      the RichMessage on its own.
    * **Fence with broken JSON** → ``rich=None`` + ``parse_error``
      diagnostic, ``fallback_text=raw_text`` (we don't strip the
      fence in this branch — better to show the raw thing than lose
      content).
    * **Multiple fences** → first one wins, others are *removed* from
      ``fallback_text`` to avoid the user seeing raw JSON.
    """
    if not raw_text:
        return ExtractResult(rich=None, fallback_text="", raw_json="")

    matches = list(_FENCE_RE.finditer(raw_text))
    if not matches:
        return ExtractResult(rich=None, fallback_text=raw_text.strip(), raw_json="")

    # First fence is our payload; subsequent ones are stripped from
    # the fallback to avoid leaking JSON into the human-visible reply.
    primary = matches[0]
    body = primary.group("body").strip()
    rich, parse_error = _parse_body(body)

    # Build fallback by removing ALL matching fences (whether or not
    # their JSON parsed). On parse failure we keep the original text
    # so the user still sees what the model produced.
    if rich is not None:
        fallback = _FENCE_RE.sub("", raw_text).strip()
        # Collapse runs of blank lines that the fence removal may have
        # left behind. Two newlines is a paragraph break — anything
        # more is awkward whitespace.
        fallback = re.sub(r"\n{3,}", "\n\n", fallback)
        # Ensure RichMessage has a non-empty fallback_text — it's the
        # safety net for renderers that can't speak rich at all.
        if rich.fallback_text == "":
            rich = _with_fallback_text(rich, fallback)
        return ExtractResult(
            rich=rich,
            fallback_text=fallback,
            raw_json=body,
            parse_error=None,
        )

    # JSON parse failed — keep the original (fences and all) so the
    # user doesn't lose anything; flag for the agent log.
    logger.debug("[rich] fence parse failed: {}", parse_error)
    return ExtractResult(
        rich=None,
        fallback_text=raw_text.strip(),
        raw_json=body,
        parse_error=parse_error,
    )


def _parse_body(body: str) -> tuple[Optional[RichMessage], Optional[str]]:
    """JSON-decode ``body`` and project to :class:`RichMessage`.

    Catches every plausible failure mode (json decode, type error,
    missing-field error from RichBlock.from_dict, etc) and returns a
    short diagnostic so the surrounding extract function can keep the
    fallback path consistent.
    """
    if not body.strip():
        return None, "empty fence body"
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return None, f"json: {exc.msg} at line {exc.lineno} col {exc.colno}"
    if not isinstance(data, dict):
        return None, f"top-level must be object, got {type(data).__name__}"
    try:
        rich = RichMessage.from_dict(data)
    except (ValueError, TypeError) as exc:
        return None, f"schema: {exc}"
    if rich.is_empty():
        return None, "rich payload has no title / blocks / fallback"
    return rich, None


def _with_fallback_text(rich: RichMessage, fallback_text: str) -> RichMessage:
    """Return a copy of ``rich`` with ``fallback_text`` populated.

    Plain dataclass replace would be cleaner but RichMessage's
    ``__post_init__`` re-coerces blocks; calling it twice on the same
    list would be wasteful. We mutate in place because ExtractResult
    is the sole owner at this point.
    """
    rich.fallback_text = fallback_text
    return rich
