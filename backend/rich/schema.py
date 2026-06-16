"""Cross-platform rich message data model (v0.38).

The agent layer always produces a :class:`RichMessage` regardless of
which IM gateway is going to deliver it. Per-gateway renderers
(see ``backend/rich/renderers``) translate the structured form to
whatever representation the underlying channel actually supports —
markdown for wecom_bot, plain-text-aligned-tables for weixin's iLink
text-only protocol, etc.

Why a single, deliberately small block taxonomy
-----------------------------------------------

Every IM platform has its own rich-message dialect:

* 飞书 (Lark) Interactive Card — JSON, headers, dividers, columns.
* 企业微信群机器人 — markdown / template_card / news.
* 微信 公众号 客服消息 — text / image / news / wxcard.
* 微信 个人号 (iLink) — text only (no public image item kind).
* Telegram — markdown_v2 / html / inline keyboard.
* Discord — embeds / components.

If we let the LLM emit one of those dialects directly we tie the prompt
to a specific platform; if we let it emit free markdown the platform
adapter has to re-parse everything. The middle ground is a tiny shared
schema: the LLM emits a ``rich-content`` JSON fence in the *same*
shape regardless of channel, and each gateway maps blocks onto its
native primitives. Failing renderers always fall back to
:attr:`RichMessage.fallback_text` so we never lose the answer.

Block kinds — chosen on purpose
-------------------------------

* ``paragraph`` — a free-form paragraph (the default fallback).
* ``kv`` — key/value rows (table-of-1) — perfect for "车次 / 日期 /
  价格"; survives degradation because we can right-pad keys to align.
* ``table`` — small N-column table (route timetables, fee breakdowns).
* ``bullets`` — bullet list (注意事项 / 推荐理由).
* ``highlight`` — a "callout" box (重要提示 / 风险警告).
* ``divider`` — visual separator between sections.
* ``link`` — link card with optional title / image_url (recommendations).

We intentionally do **not** model headings, multi-column layouts, or
forms. Anything more complex is composed from this set; anything
unmodelled belongs in fallback_text. Adding a kind later is cheap
(every renderer falls through unknown kinds to a paragraph), but
removing one is painful, so the bar is high.

Roundtrip rules
---------------

* :meth:`RichMessage.to_dict` produces a JSON-safe dict that can go
  straight into the wiki cache or a manifest fixture. ``meta`` and
  ``raw`` payloads must already be JSON-clean.
* :meth:`RichMessage.from_dict` is permissive: unknown block kinds
  pass through (the renderer will degrade), unknown top-level fields
  are kept under ``meta``. We don't raise on missing fields — a
  zero-block RichMessage is legal (means "fallback only").
* Equality is structural; ``__hash__`` is intentionally not provided
  because instances are mutable lists.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Block kind tokens. Use module-level constants so import sites get an
# IDE autocomplete + a single source of truth for the renderer dispatch
# tables. Strings (not enums) because the wire format is JSON.
BLOCK_PARAGRAPH = "paragraph"
BLOCK_KV = "kv"
BLOCK_TABLE = "table"
BLOCK_BULLETS = "bullets"
BLOCK_HIGHLIGHT = "highlight"
BLOCK_DIVIDER = "divider"
BLOCK_LINK = "link"
BLOCK_KINDS: tuple[str, ...] = (
    BLOCK_PARAGRAPH,
    BLOCK_KV,
    BLOCK_TABLE,
    BLOCK_BULLETS,
    BLOCK_HIGHLIGHT,
    BLOCK_DIVIDER,
    BLOCK_LINK,
)


@dataclass(slots=True)
class RichBlock:
    """A single content block within a :class:`RichMessage`.

    The ``kind`` field controls which other fields the renderer reads
    — see the block-kind documentation in the module docstring above
    for the canonical contract:

    +----------------+-----------------------------------------------+
    | kind           | populated fields                              |
    +================+===============================================+
    | ``paragraph``  | ``text``                                      |
    +----------------+-----------------------------------------------+
    | ``kv``         | ``title`` (opt), ``pairs``                    |
    +----------------+-----------------------------------------------+
    | ``table``      | ``title`` (opt), ``columns``, ``rows``        |
    +----------------+-----------------------------------------------+
    | ``bullets``    | ``title`` (opt), ``items``                    |
    +----------------+-----------------------------------------------+
    | ``highlight``  | ``text`` (the callout body)                   |
    +----------------+-----------------------------------------------+
    | ``divider``    | (no fields read)                              |
    +----------------+-----------------------------------------------+
    | ``link``       | ``text`` (label), ``url``, ``image_url`` opt  |
    +----------------+-----------------------------------------------+

    Renderers must tolerate missing optional fields and degrade
    unknown kinds to a paragraph rendering of any present ``text``.
    """

    kind: str
    title: str | None = None
    text: str | None = None
    # ``pairs`` is List[[key, value]]; both halves are strings. Using
    # plain lists (not tuples) so it round-trips through JSON without
    # custom encoding.
    pairs: list[list[str]] = field(default_factory=list)
    # ``columns`` is the header row for ``table``; ``rows`` is the
    # body. We allow a missing ``columns`` (renderers print body-only).
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    # ``items`` for bullets.
    items: list[str] = field(default_factory=list)
    # ``link`` companions — both optional so we can also use them
    # decoratively under other kinds (e.g. an image_url under
    # highlight is allowed; renderers that can't show it ignore it).
    url: str | None = None
    image_url: str | None = None
    # Free-form per-block extensions. Renderers should never put
    # required information here — anything callers can't drop into
    # one of the structured fields belongs in the surrounding
    # ``RichMessage.meta`` instead.
    meta: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Roundtrip
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-clean projection. Empty fields are *omitted* so the
        wire form stays compact (a ``divider`` is just
        ``{"kind": "divider"}``)."""
        out: dict[str, Any] = {"kind": self.kind}
        if self.title:
            out["title"] = self.title
        if self.text is not None and self.text != "":
            out["text"] = self.text
        if self.pairs:
            out["pairs"] = [list(p) for p in self.pairs]
        if self.columns:
            out["columns"] = list(self.columns)
        if self.rows:
            out["rows"] = [list(r) for r in self.rows]
        if self.items:
            out["items"] = list(self.items)
        if self.url:
            out["url"] = self.url
        if self.image_url:
            out["image_url"] = self.image_url
        if self.meta:
            out["meta"] = dict(self.meta)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RichBlock":
        """Permissive parser. Unknown kinds pass through; missing
        fields default to empty. Any non-list iterable for ``pairs``,
        ``rows``, etc. is coerced to a list of lists of strings so
        downstream renderers don't trip on tuples or generators."""
        if not isinstance(data, dict):
            raise ValueError(f"RichBlock.from_dict expects dict, got {type(data)!r}")
        kind = str(data.get("kind", BLOCK_PARAGRAPH)).strip() or BLOCK_PARAGRAPH

        def _list_of_str(value: Any) -> list[str]:
            if not value:
                return []
            return [str(item) for item in value]

        def _list_of_pair(value: Any) -> list[list[str]]:
            if not value:
                return []
            out: list[list[str]] = []
            for item in value:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    out.append([str(item[0]), str(item[1])])
            return out

        def _list_of_row(value: Any) -> list[list[str]]:
            if not value:
                return []
            return [_list_of_str(row) for row in value]

        title = data.get("title")
        text = data.get("text")
        return cls(
            kind=kind,
            title=str(title) if title not in (None, "") else None,
            text=str(text) if text is not None else None,
            pairs=_list_of_pair(data.get("pairs")),
            columns=_list_of_str(data.get("columns")),
            rows=_list_of_row(data.get("rows")),
            items=_list_of_str(data.get("items")),
            url=str(data["url"]) if data.get("url") else None,
            image_url=str(data["image_url"]) if data.get("image_url") else None,
            meta=dict(data["meta"]) if isinstance(data.get("meta"), dict) else {},
        )


@dataclass(slots=True)
class RichMessage:
    """Channel-agnostic rich answer.

    A :class:`RichMessage` always carries a :attr:`fallback_text` —
    that's what the gateway sends when its renderer does not support
    rich content (or when rendering fails for any reason). Empty
    fallback_text is legal but discouraged; the agent layer fills it
    in before dispatch as a defence in depth.
    """

    title: str | None = None
    subtitle: str | None = None
    blocks: list[RichBlock] = field(default_factory=list)
    fallback_text: str = ""
    # Free-form metadata — provenance fields, click-tracking ids,
    # anything that should travel with the message but doesn't render.
    # Must be JSON-serialisable; the wiki cache stores it verbatim.
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Guard against a common LLM-extraction failure mode: blocks
        # array contained dicts and the agent layer forgot to map
        # them into RichBlocks. Coerce defensively so the renderer
        # never sees raw dicts.
        coerced: list[RichBlock] = []
        for blk in self.blocks:
            if isinstance(blk, RichBlock):
                coerced.append(blk)
            elif isinstance(blk, dict):
                coerced.append(RichBlock.from_dict(blk))
            else:
                raise TypeError(
                    f"RichMessage.blocks must contain RichBlock or dict, got {type(blk)!r}"
                )
        self.blocks = coerced

    # ------------------------------------------------------------------
    # Roundtrip
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.title:
            out["title"] = self.title
        if self.subtitle:
            out["subtitle"] = self.subtitle
        if self.blocks:
            out["blocks"] = [b.to_dict() for b in self.blocks]
        if self.fallback_text:
            out["fallback_text"] = self.fallback_text
        if self.meta:
            out["meta"] = dict(self.meta)
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RichMessage":
        if not isinstance(data, dict):
            raise ValueError(
                f"RichMessage.from_dict expects dict, got {type(data)!r}"
            )
        blocks_raw = data.get("blocks") or []
        if not isinstance(blocks_raw, list):
            raise ValueError("RichMessage.blocks must be a list")
        blocks = [RichBlock.from_dict(b) for b in blocks_raw if isinstance(b, dict)]
        return cls(
            title=str(data["title"]) if data.get("title") else None,
            subtitle=str(data["subtitle"]) if data.get("subtitle") else None,
            blocks=blocks,
            fallback_text=str(data.get("fallback_text") or ""),
            meta=dict(data["meta"]) if isinstance(data.get("meta"), dict) else {},
        )

    @classmethod
    def from_json(cls, text: str) -> "RichMessage":
        return cls.from_dict(json.loads(text))

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def is_empty(self) -> bool:
        """True when there's literally nothing to render (no blocks,
        no title, no subtitle, no fallback). Renderers can shortcut
        on this so an empty rich payload doesn't fan out to a blank
        message."""
        return (
            not self.title
            and not self.subtitle
            and not self.blocks
            and not self.fallback_text
        )
