from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

from loguru import logger

from ...domains.travel.routing import (
    _build_realtime_loading_hint,
    _should_route_realtime_travel,
)
from ...gateways.base import IncomingMessage
from .inheritance import is_confirmation_only, should_inherit_skill_hint
from .skill_match import pick_skill_for_message

if TYPE_CHECKING:
    from ...memory.manager import MemoryManager
    from ...skills.loader import SkillLoader
    from .llm_router import RouterDecision, RouterLLM


_ACK_FIRST_MARKERS = (
    "\u6bcf\u5929",  # every day
    "\u6bcf\u5468",  # every week
    "\u6bcf\u6708",  # every month
    "\u6bcf\u9694",  # every N interval
    "\u6c47\u62a5",  # report
    "\u65e5\u62a5",  # daily report
    "\u5468\u62a5",  # weekly report
    "daily",
    "weekly",
    "report",
)

_MCP_MANAGE_MARKERS = (
    "mcp",
    "mcp server",
    "mcp_manage",
    "安装",
    "接入",
    "添加",
    "卸载",
    "删除",
    "移除",
    "禁用",
    "启用",
    "装一个",
    "装一下",
    "找一个工具",
    "找个工具",
    "工具",
)


@dataclass(slots=True)
class SkillRoute:
    skill_id: Optional[str] = None
    manifest: Optional[Any] = None
    source: str = "none"
    realtime_travel: bool = False


def should_ack_first(message: IncomingMessage) -> bool:
    if message.platform != "weixin" or message.reply_target is None:
        return False
    text = (message.text or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _ACK_FIRST_MARKERS)


def should_route_realtime_travel(text: str) -> bool:
    return _should_route_realtime_travel(text)


def realtime_loading_hint(text: str) -> str:
    return _build_realtime_loading_hint(text)


class TurnRoutingPolicy:
    def __init__(
        self,
        *,
        skill_loader: Optional["SkillLoader"] = None,
        memory_manager: Optional["MemoryManager"] = None,
        router_llm: Optional["RouterLLM"] = None,
        workspace_dir: Optional[Path] = None,
        realtime_predicate: Callable[[str], bool] = should_route_realtime_travel,
        realtime_hint_builder: Callable[[str], str] = realtime_loading_hint,
    ) -> None:
        self._skill_loader = skill_loader
        self._memory = memory_manager
        self._router_llm = router_llm
        self._workspace_dir = Path(workspace_dir).resolve() if workspace_dir else None
        self._realtime_predicate = realtime_predicate
        self._realtime_hint_builder = realtime_hint_builder

    async def route_intent(self, user_text: str) -> "RouterDecision":
        if self._router_llm is None:
            from .llm_router import empty_decision
            return empty_decision("disabled")
        return await self._router_llm.route(
            user_text,
            modes=self.build_router_modes_catalog(),
            skills=self.build_router_skills_catalog(),
        )

    def build_router_modes_catalog(self) -> list:
        if self._workspace_dir is None:
            return []
        from .llm_router import ModeCatalogEntry
        from ...tools.builtins.knowledge_mode_manage import _read_mode_meta
        root = self._workspace_dir / "knowledge_modes"
        if not root.exists():
            return []
        entries: list = []
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            mode_md = path / "MODE.md"
            if not mode_md.exists():
                continue
            meta = _read_mode_meta(mode_md)
            entries.append(ModeCatalogEntry(
                mode_id=path.name,
                template=str(meta.get("template", "unknown")),
                title=str(meta.get("title") or path.name),
                description=str(meta.get("description") or ""),
            ))
        return entries

    def build_router_skills_catalog(self) -> list:
        if self._skill_loader is None:
            return []
        from .llm_router import SkillCatalogEntry
        entries: list = []
        for manifest in self._skill_loader.list():
            entries.append(SkillCatalogEntry(
                skill_id=manifest.id,
                name=manifest.name,
                description=manifest.description,
                tags=tuple(manifest.tags),
            ))
        return entries

    def pick_skill_route(self, safe_text: str, session_id: str) -> SkillRoute:
        if self._skill_loader is None:
            return SkillRoute()

        skill_id = None
        manifest = None
        source = "none"
        text_lower = safe_text.lower()
        mcp_manage_intent = any(marker in text_lower for marker in _MCP_MANAGE_MARKERS)

        if not mcp_manage_intent:
            skill_id = pick_skill_for_message(safe_text, self._skill_loader.list())
            if skill_id is not None:
                manifest = self._skill_loader.get(skill_id)
                source = "trigger"
        else:
            logger.info(
                "[skill] bypassed normal skill trigger routing for MCP-management intent: {!r}",
                safe_text[:80],
            )

        if skill_id is None and self._memory is not None:
            inherited = self._memory.latest_skill_hint(session_id=session_id)
            if inherited and is_confirmation_only(safe_text):
                logger.info(
                    "[skill] blocked inheriting '{}' for confirmation-only message: {!r}",
                    inherited, safe_text[:60],
                )
            elif inherited and should_inherit_skill_hint(safe_text, inherited):
                candidate = self._skill_loader.get(inherited)
                if candidate is not None:
                    skill_id, manifest = inherited, candidate
                    source = "inherit"
                    logger.info(
                        "[skill] inherited '{}' from prior turn for short follow-up: {!r}",
                        inherited, safe_text[:60],
                    )
            elif inherited:
                logger.info(
                    "[skill] skipped inheriting '{}' for non-slot-fill message: {!r}",
                    inherited, safe_text[:60],
                )

        realtime_travel = self._realtime_predicate(safe_text)
        if realtime_travel:
            realtime = self._skill_loader.get("travel-realtime-mcp")
            if realtime is not None and skill_id != "travel-realtime-mcp":
                logger.info(
                    "[skill] realtime travel override: {} -> travel-realtime-mcp for {!r}",
                    skill_id or "(none)", safe_text[:80],
                )
                return SkillRoute(
                    skill_id="travel-realtime-mcp",
                    manifest=realtime,
                    source="realtime_travel",
                    realtime_travel=True,
                )
        return SkillRoute(
            skill_id=skill_id,
            manifest=manifest,
            source=source,
            realtime_travel=realtime_travel,
        )

    def should_realtime_prefetch(
        self,
        safe_text: str,
        routed_skill_id: Optional[str],
        *,
        has_travel_realtime_tool: bool,
    ) -> bool:
        return (
            routed_skill_id == "travel-realtime-mcp"
            and has_travel_realtime_tool
            and self._realtime_predicate(safe_text)
        )

    def realtime_loading_hint(self, safe_text: str) -> str:
        return self._realtime_hint_builder(safe_text)
