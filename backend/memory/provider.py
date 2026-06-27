from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class MemoryProvider(Protocol):
    name: str

    def initialize(self, **kwargs: Any) -> None: ...

    def system_prompt_block(self) -> str: ...

    def prefetch(self, query: str, *, session_id: str = "", **kwargs: Any) -> str: ...

    def latest_skill_hint(self, *, session_id: str = "", **kwargs: Any) -> str: ...

    def queue_prefetch(self, query: str, *, session_id: str = "", **kwargs: Any) -> None: ...

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None: ...

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None: ...

    def reset_session(
        self,
        *,
        session_id: str = "",
        reason: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> str: ...

    def on_pre_compress(self, history: list[Any]) -> str: ...

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None: ...

    def shutdown(self) -> None: ...


class BaseMemoryProvider:
    name = "base"

    def initialize(self, **kwargs: Any) -> None:
        return None

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "", **kwargs: Any) -> str:
        return ""

    def latest_skill_hint(self, *, session_id: str = "", **kwargs: Any) -> str:
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "", **kwargs: Any) -> None:
        return None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        return None

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        return None

    def reset_session(
        self,
        *,
        session_id: str = "",
        reason: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        return ""

    def on_pre_compress(self, history: list[Any]) -> str:
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        return None

    def shutdown(self) -> None:
        return None
