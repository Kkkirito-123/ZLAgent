"""Plain-text renderer for :class:`RichMessage` (v0.38).

Targeted at channels that can ONLY display plain text — most notably
the WeChat 个人号 / 公众号 iLink path which has no native image / card
type. We compensate by:

* Right-padding KV keys + table columns with **full-width** spaces so
  Chinese clients (which use a roughly square em-box for both Latin
  and CJK characters) keep columns aligned. Half-width spaces look
  cramped because the WeChat font isn't truly monospace.
* Expressing dividers as a row of light box-drawing characters, which
  most CJK fonts render at the correct width.
* Emitting one logical message per :class:`RichMessage` (the agent's
  surrounding code is responsible for any rate-limited fan-out, this
  renderer never returns a list).

Why not just use markdown
-------------------------

WeChat 公众号 and most personal-account channels render markdown as
literal text — the user sees ``** ... **`` instead of bold. Reusing
the markdown renderer here would just leak punctuation. The
wecom_bot renderer (which DOES support markdown) lives in a separate
module precisely to keep this distinction explicit.

Width math
----------

Each character contributes a "display width" in CJK terminals:

* ASCII / Latin / digits / common punctuation → 1
* CJK ideographs / 全角标点 → 2
* Emoji → 2 (with rare exceptions; we don't try to be exhaustive)
* Whitespace tabs / control → 1 each (we never emit tabs)

We use :func:`_visual_width` to compute that, then pad to the widest
key in a KV block / column in a table block. The pad character is
``\u3000`` (CJK ideographic space) which renders at the same width as
a CJK character, so 1 pad-char fills 2 visual columns. We round up
when the gap is odd; ASCII keys get an extra half-width space.
"""
from __future__ import annotations

import unicodedata
from typing import Iterable

from ..schema import (
    BLOCK_BULLETS,
    BLOCK_DIVIDER,
    BLOCK_HIGHLIGHT,
    BLOCK_KV,
    BLOCK_LINK,
    BLOCK_PARAGRAPH,
    BLOCK_TABLE,
    RichBlock,
    RichMessage,
)


# Width helpers --------------------------------------------------------

def _char_width(ch: str) -> int:
    """Visual width of a single character in a CJK terminal.

    Based on Unicode East Asian Width property: F (Fullwidth),
    W (Wide), A (Ambiguous when in a CJK font) → 2 columns; everything
    else → 1. We treat Ambiguous as wide because the dominant audience
    runs CJK fonts; for pure-Latin clients the numbers are still off
    by at most one space.
    """
    if not ch:
        return 0
    ea = unicodedata.east_asian_width(ch)
    if ea in ("F", "W"):
        return 2
    if ea == "A":
        # Ambiguous — treat as wide for CJK rendering. Most emoji
        # also fall through here.
        return 2
    # Treat private-use characters used by emoji (e.g. flags) as wide.
    if ord(ch) >= 0x2600 and ord(ch) <= 0x27BF:
        return 2
    return 1


def _visual_width(text: str) -> int:
    return sum(_char_width(c) for c in text)


def _pad_to(text: str, target: int) -> str:
    """Pad ``text`` with full-width spaces (and one ASCII space for
    parity) so its visual width equals or just exceeds ``target``."""
    gap = target - _visual_width(text)
    if gap <= 0:
        return text
    full = gap // 2
    half = gap - full * 2
    return text + ("\u3000" * full) + (" " * half)


# Per-block renderers --------------------------------------------------

def _render_paragraph(blk: RichBlock) -> str:
    return (blk.text or "").strip()


def _render_kv(blk: RichBlock) -> str:
    if not blk.pairs:
        return blk.title or ""
    # Compute target width: max key width + a 2-col gap so the values
    # don't run into the keys.
    max_key = max((_visual_width(k) for k, _ in blk.pairs), default=0)
    target = max_key + 2
    lines: list[str] = []
    if blk.title:
        lines.append(f"【{blk.title}】")
    for key, value in blk.pairs:
        lines.append(f"{_pad_to(str(key), target)}{value}")
    return "\n".join(lines)


def _render_table(blk: RichBlock) -> str:
    if not blk.rows:
        return blk.title or ""
    columns = blk.columns or []
    body_rows = [[str(cell) for cell in row] for row in blk.rows]
    # Column-wise width: max of header + every cell. Pad with 2 cols
    # of breathing room.
    n_cols = max(
        len(columns),
        max((len(r) for r in body_rows), default=0),
    )
    widths: list[int] = [0] * n_cols
    if columns:
        for i, h in enumerate(columns[:n_cols]):
            widths[i] = max(widths[i], _visual_width(str(h)))
    for row in body_rows:
        for i in range(min(len(row), n_cols)):
            widths[i] = max(widths[i], _visual_width(row[i]))
    target_widths = [w + 2 for w in widths]

    def _format_row(row: Iterable[str]) -> str:
        cells = list(row)
        out: list[str] = []
        for i in range(n_cols):
            cell = cells[i] if i < len(cells) else ""
            # Don't pad the last column — trailing whitespace is ugly
            # and most clients trim it.
            if i == n_cols - 1:
                out.append(cell)
            else:
                out.append(_pad_to(cell, target_widths[i]))
        return "".join(out).rstrip()

    lines: list[str] = []
    if blk.title:
        lines.append(f"【{blk.title}】")
    if columns:
        lines.append(_format_row(columns))
        # Underline header with single-line box drawings so the column
        # split is visually obvious without breaking width.
        lines.append("─" * min(40, sum(target_widths)))
    for row in body_rows:
        lines.append(_format_row(row))
    return "\n".join(lines)


def _render_bullets(blk: RichBlock) -> str:
    items = [str(i) for i in blk.items if str(i).strip()]
    if not items:
        return blk.title or ""
    lines: list[str] = []
    if blk.title:
        lines.append(f"【{blk.title}】")
    for item in items:
        lines.append(f"• {item}")
    return "\n".join(lines)


def _render_highlight(blk: RichBlock) -> str:
    body = (blk.text or "").strip()
    if not body:
        return ""
    # Surround with a thin box-drawing border so callouts stand out
    # without relying on bold / colour.
    return f"┌─ 重点 ─\n│ {body}\n└──"


def _render_divider() -> str:
    return "──────"


def _render_link(blk: RichBlock) -> str:
    label = (blk.text or blk.title or blk.url or "").strip()
    if not blk.url:
        return label
    if not label or label == blk.url:
        return blk.url
    return f"{label}：{blk.url}"


_BLOCK_RENDERERS = {
    BLOCK_PARAGRAPH: _render_paragraph,
    BLOCK_KV: _render_kv,
    BLOCK_TABLE: _render_table,
    BLOCK_BULLETS: _render_bullets,
    BLOCK_HIGHLIGHT: _render_highlight,
    BLOCK_LINK: _render_link,
}


# Top-level entry ------------------------------------------------------

def render_plain_text(rich: RichMessage) -> str:
    """Render a :class:`RichMessage` to a single plain-text string.

    Always returns a non-empty string when at least one of
    ``title / subtitle / blocks / fallback_text`` is set; empty input
    produces an empty string.

    Block separator is two newlines (a paragraph break); the divider
    block emits a horizontal rule. Failure modes (unknown kind, etc)
    degrade to ``fallback_text`` for the OFFENDING block only — we
    don't bail on the whole message just because one block was odd.
    """
    if rich.is_empty():
        return ""

    parts: list[str] = []
    if rich.title:
        parts.append(rich.title.strip())
    if rich.subtitle:
        parts.append(rich.subtitle.strip())

    for blk in rich.blocks:
        try:
            if blk.kind == BLOCK_DIVIDER:
                rendered = _render_divider()
            else:
                rendered = _BLOCK_RENDERERS.get(
                    blk.kind, _render_paragraph
                )(blk)
        except Exception:  # noqa: BLE001 - render must never raise
            rendered = (blk.text or "").strip()
        rendered = rendered.strip()
        if rendered:
            parts.append(rendered)

    if not parts:
        return rich.fallback_text.strip()

    return "\n\n".join(parts)
