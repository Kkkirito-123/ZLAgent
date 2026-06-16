"""Per-platform :class:`RichMessage` renderers.

Each renderer is a pure function ``render(rich) -> str``; the gateway
imports the renderer for its platform and uses the returned string
verbatim as the message body. Failure modes (unknown block kind,
missing field) degrade to either a plain-text rendering of the
present content or to ``rich.fallback_text``.

Rendering must NOT raise. If a renderer cannot produce a string for
*any* reason it must return ``rich.fallback_text``; the agent log
captures the diagnostic separately.
"""
from .text import render_plain_text
from .wecom_bot import render_wecom_bot_markdown

__all__ = ["render_plain_text", "render_wecom_bot_markdown"]
