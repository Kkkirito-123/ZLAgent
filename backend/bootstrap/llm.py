from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from loguru import logger

from ..llm import LLMClient

if TYPE_CHECKING:
    from ..core.config import Settings
    from ..agent.routing.llm_router import RouterLLM


def build_llm(settings: "Settings") -> LLMClient:
    client = LLMClient(settings)
    if client.configured:
        logger.info("llm provider={} model={} base_url={}", client.provider, client.model, client.base_url)
    else:
        logger.info("llm not configured (set OPENAI_API_KEY + OPENAI_MODEL or OPENAI_BASE_URL to enable LLM-generated replies); falling back to echo")
    return client


def build_router_llm(settings: "Settings", llm_client: LLMClient) -> Optional["RouterLLM"]:
    if not settings.router_llm_enabled:
        return None
    if not settings.router_llm_model.strip():
        logger.warning("router-llm enabled but ``router_llm_model`` is empty — disabling. Set ZLAGENT_ROUTER_LLM_MODEL to e.g. ``deepseek-v4-flash``.")
        return None
    from ..agent.routing.llm_router import RouterLLM, build_router_settings
    router_settings = build_router_settings(settings)
    router_client = LLMClient(router_settings)
    if not router_client.configured:
        logger.warning(
            "router-llm requested (model={}) but the derived client is not configured — disabling."
            " Check OPENAI_API_KEY / OPENAI_BASE_URL or router_llm_api_key / router_llm_base_url.",
            settings.router_llm_model,
        )
        return None
    router = RouterLLM(
        router_client,
        timeout_seconds=settings.router_llm_timeout_seconds,
        cache_ttl_seconds=settings.router_llm_cache_ttl_seconds,
    )
    logger.info("router-llm enabled model={} base_url={} timeout={}s",
                router_client.model, router_client.base_url, settings.router_llm_timeout_seconds)
    return router
