"""Turn preparation data models.

The runtime package used to own these types, but the agent package now
hosts the canonical definitions to avoid import cycles during startup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(slots=True)
class PreparedTurn:
    """Turn payload assembled by the preparer and consumed by the loop."""

    safe_text: str
    session_id: str
    platform: str
    user_id: str
    reply_target: Any
    stable_prompt: str
    dynamic_suffix: str
    history: list[Any]
    system_prompt: str = ""
    routed_skill_id: Optional[str] = None
    routed_manifest: Any = None
    memory_intent: Any = None
    streaming_skill: bool = False
    dispatch_fn: Any = None
    early_reply: Any = None


@dataclass(slots=True)
class PostTurnEvent:
    """Marker for post-turn processing hooks."""

    name: str
    payload: dict[str, Any] | None = None


__all__ = ["PreparedTurn", "PostTurnEvent"]
