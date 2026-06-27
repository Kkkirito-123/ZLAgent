from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from ...wiki.geo_store import GeoStore
    from ...wiki.store import WikiHit, WikiStore


class WikiRetrievalPolicy:
    def __init__(
        self,
        *,
        wiki_store: Optional["WikiStore"] = None,
        geo_store: Optional["GeoStore"] = None,
    ) -> None:
        self._wiki_store = wiki_store
        self._geo_store = geo_store

    def should_lookup_fast_path(
        self,
        *,
        skill_id: Optional[str],
        manifest: Optional[Any],
        streaming_to_im: bool,
    ) -> bool:
        return (
            skill_id is not None
            and manifest is not None
            and bool(getattr(manifest, "wiki_cache_enabled", False))
            and not (bool(getattr(manifest, "streaming_sections", ())) and streaming_to_im)
            and self._wiki_store is not None
        )

    def lookup_fast_path(
        self,
        *,
        skill_id: Optional[str],
        manifest: Optional[Any],
        query: str,
        streaming_to_im: bool,
    ) -> Optional["WikiHit"]:
        if not self.should_lookup_fast_path(
            skill_id=skill_id,
            manifest=manifest,
            streaming_to_im=streaming_to_im,
        ):
            return None
        assert skill_id is not None
        return self.lookup(skill_id, query)

    def lookup(self, skill_id: str, query: str) -> Optional["WikiHit"]:
        if self._wiki_store is None:
            return None
        nodes: list = []
        if self._geo_store is not None:
            try:
                nodes = self._geo_store.detect_in_text(query, max_matches=4)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[geo] detect_in_text failed for query={!r}: {}",
                    query[:80], exc,
                )
                nodes = []

        for node in nodes:
            if node.type != "city":
                continue
            geo_store = self._geo_store
            if geo_store is None:
                continue
            geo_token = geo_store.geo_path_for(node)
            try:
                hit = self._wiki_store.lookup(
                    skill_id, query, geo_filter=geo_token,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wiki geo lookup failed skill={} geo={}: {}; trying next",
                    skill_id, geo_token, exc,
                )
                continue
            if hit is not None:
                logger.info(
                    "[wiki] geo-narrowed HIT skill={} geo={} kind={}",
                    skill_id, geo_token, hit.similarity_kind,
                )
                return hit

        return self._wiki_store.lookup(skill_id, query)
