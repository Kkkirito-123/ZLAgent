"""rich content / structured cards across IM platforms.

The agent layer produces a single :class:`RichMessage` ; per-gateway
renderers convert it to the best representation that the underlying
channel supports (e.g. wecom_bot's ``markdown`` msgtype, weixin's
plain-text-with-aligned-tables, etc).

See ``schema.py`` for the data model and ``parser.py`` for the
fence-based LLM extractor.
"""
from .schema import (
    BLOCK_BULLETS,
    BLOCK_DIVIDER,
    BLOCK_HIGHLIGHT,
    BLOCK_KV,
    BLOCK_LINK,
    BLOCK_PARAGRAPH,
    BLOCK_TABLE,
    BLOCK_KINDS,
    RichBlock,
    RichMessage,
)

__all__ = [
    "BLOCK_BULLETS",
    "BLOCK_DIVIDER",
    "BLOCK_HIGHLIGHT",
    "BLOCK_KV",
    "BLOCK_LINK",
    "BLOCK_PARAGRAPH",
    "BLOCK_TABLE",
    "BLOCK_KINDS",
    "RichBlock",
    "RichMessage",
]
