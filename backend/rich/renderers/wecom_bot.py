"""企业微信群机器人 markdown renderer for :class:`RichMessage`.

WeCom group bots support a ``markdown`` msgtype with a constrained
subset:

  * Headings (`#` through `######`)
  * Bold (`**...**`), italic NOT supported, strikethrough NOT supported
  * Inline code, fenced code (```)
  * Links `[text](url)`
  * Quotes (`> ...`)
  * Lists (ordered / unordered)
  * Coloured comments via `<font color="info|comment|warning">...</font>`

Tables, images, dividers (---) and emoji shortcodes are NOT
guaranteed; in practice the official renderer handles GFM tables but
strips horizontal rules. We emit GFM tables anyway because they look
right on the desktop and degrade to legible text on mobile.

Reference:
  https://developer.work.weixin.qq.com/document/path/91770

Cap on body length: 4096 bytes (UTF-8). We don't enforce here — the
gateway raises if the API rejects oversize content. RichMessage
authors should keep blocks small (the design assumption is
recommendation cards / itineraries, not novellas).
"""
from __future__ import annotations

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


def _md_escape(text: str) -> str:
    """Escape just enough characters so user content doesn't get
    interpreted as markdown formatting. We deliberately don't escape
    `*` or `_` because keeping them readable in plain-text fallback
    matters more than blocking formatting injection (LLM output is
    not user-attacker-controlled in our threat model)."""
    return text.replace("|", "\\|").replace("\\", "\\\\")


def _render_paragraph(blk: RichBlock) -> str:
    return (blk.text or "").strip()


def _render_kv(blk: RichBlock) -> str:
    if not blk.pairs:
        return f"**{blk.title}**" if blk.title else ""
    lines: list[str] = []
    if blk.title:
        lines.append(f"**{blk.title}**")
    for key, value in blk.pairs:
        # WeCom's markdown handles `**` correctly; bolding the key
        # makes scanning easier on the mobile preview.
        lines.append(f"- **{_md_escape(str(key))}**：{_md_escape(str(value))}")
    return "\n".join(lines)


def _render_table(blk: RichBlock) -> str:
    if not blk.rows:
        return f"**{blk.title}**" if blk.title else ""
    columns = list(blk.columns) if blk.columns else []
    body_rows = [[str(c) for c in row] for row in blk.rows]
    n_cols = max(len(columns), max((len(r) for r in body_rows), default=0))
    if not columns:
        # Fabricate empty headers so GFM tables render. Mobile clients
        # show a thin top border which is still legible.
        columns = [""] * n_cols

    def _row(row: list[str]) -> str:
        padded = list(row) + [""] * (n_cols - len(row))
        return "| " + " | ".join(_md_escape(c) for c in padded[:n_cols]) + " |"

    lines: list[str] = []
    if blk.title:
        lines.append(f"**{blk.title}**")
    lines.append(_row(columns))
    lines.append("|" + "|".join([" --- "] * n_cols) + "|")
    for row in body_rows:
        lines.append(_row(row))
    return "\n".join(lines)


def _render_bullets(blk: RichBlock) -> str:
    items = [str(i).strip() for i in blk.items if str(i).strip()]
    if not items:
        return f"**{blk.title}**" if blk.title else ""
    lines: list[str] = []
    if blk.title:
        lines.append(f"**{blk.title}**")
    for item in items:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _render_highlight(blk: RichBlock) -> str:
    body = (blk.text or "").strip()
    if not body:
        return ""
    # WeCom supports the `comment` / `warning` colour tags inside
    # `<font>` even within a quote. The combination of a quote prefix
    # + warning colour gives a clear "callout" visual.
    return f"> <font color=\"warning\">**重点**</font>\n> {body}"


def _render_divider() -> str:
    # WeCom strips ``---`` horizontal rules; emit a row of mid-dots
    # which renders identically across desktop / mobile.
    return "·" * 24


def _render_link(blk: RichBlock) -> str:
    if not blk.url:
        return (blk.text or blk.title or "").strip()
    label = (blk.text or blk.title or blk.url).strip()
    return f"[{label}]({blk.url})"


_BLOCK_RENDERERS = {
    BLOCK_PARAGRAPH: _render_paragraph,
    BLOCK_KV: _render_kv,
    BLOCK_TABLE: _render_table,
    BLOCK_BULLETS: _render_bullets,
    BLOCK_HIGHLIGHT: _render_highlight,
    BLOCK_LINK: _render_link,
}


def render_wecom_bot_markdown(rich: RichMessage) -> str:
    """Render to a string suitable for ``msgtype=markdown``.

    Always returns a string (possibly empty if ``rich`` is empty) so
    the gateway never has to special-case None. Failure on any single
    block degrades to that block's ``text`` field (or empty); other
    blocks keep rendering.
    """
    if rich.is_empty():
        return ""

    parts: list[str] = []
    if rich.title:
        parts.append(f"# {rich.title.strip()}")
    if rich.subtitle:
        parts.append(f"> {rich.subtitle.strip()}")

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
