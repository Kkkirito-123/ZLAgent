"""MCP subsystem boot helper.

Extracted in v0.40 from :func:`backend.app.lifespan` to keep the
FastAPI startup orchestration readable. Encapsulates the v0.14 +
v0.33 + v0.34 wiring:

1. Read the YAML config (best-effort — invalid YAML is logged and
   treated as empty so a typo can't down the gateway).
2. Build :class:`MCPServerStore` (the runtime overlay) and merge
   YAML + DB entries into a single config dict, with YAML winning
   on name collisions.
3. Boot :class:`MCPManager`, register every wrapper tool into the
   shared :class:`ToolRegistry`.
4. Construct :class:`MCPLifecycleService` so IM ``mcp_manage`` and
   the REST endpoints share the exact same attach/detach path.
5. Register the agent-facing ``mcp_manage`` tool.

Returns ``(manager, store, lifecycle)`` — a 3-tuple of ``None`` when
``settings.mcp_enabled`` is False so the caller can wire the disabled
state without branching twice.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from loguru import logger
from sqlalchemy.orm import sessionmaker


async def build_mcp_subsystem(
    settings: Any,
    *,
    tool_registry: Any,
    session_local: sessionmaker,
) -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """Boot the MCP subsystem and return ``(manager, store, lifecycle)``.

    All three are ``None`` when MCP is disabled so the caller can write
    ``app.state.mcp_manager = manager`` unconditionally.
    """
    if not settings.mcp_enabled:
        logger.info("[mcp] disabled via ZLAGENT_MCP_ENABLED=false")
        return None, None, None

    from . import MCPManager, load_mcp_config
    from .lifecycle import MCPLifecycleService
    from .store import MCPServerStore
    from .transport import configure_stderr_log_dir
    from .tool_wrapper import build_mcp_tools
    from ..tools.builtins import MCPManageTool

    mcp_config_path = settings.mcp_config_path or (
        settings.config_dir / "mcp_servers.yaml"
    )
    try:
        yaml_configs = load_mcp_config(mcp_config_path)
    except ValueError as exc:
        logger.error("[mcp] config invalid at {}: {}", mcp_config_path, exc)
        yaml_configs = {}

    # Build the store first so we can read the runtime overlay before
    # starting the manager — this keeps the YAML / DB merge logic in
    # one place and ensures the manager's _configs dict matches the
    # union of both sources from t=0.
    mcp_store = MCPServerStore(session_local)
    configs: dict[str, Any] = dict(yaml_configs)
    for stored in mcp_store.list_all():
        if stored.config.name in configs:
            logger.warning(
                "[mcp] DB-stored server '{}' shadows the YAML entry;"
                " keeping the YAML version (edit YAML or remove the"
                " DB row to resolve)",
                stored.config.name,
            )
            continue
        configs[stored.config.name] = stored.config

    configure_stderr_log_dir(settings.workspace_dir)
    mcp_manager = MCPManager(
        configs,
        breaker_threshold=settings.mcp_breaker_threshold,
        breaker_cooldown_seconds=settings.mcp_breaker_cooldown_seconds,
    )
    await mcp_manager.start()
    if configs:
        mcp_tools = build_mcp_tools(mcp_manager)
        for tool in mcp_tools:
            try:
                tool_registry.register(tool)
            except ValueError as exc:
                logger.warning("[mcp] could not register {}: {}", tool.name, exc)
        logger.info(
            "[mcp] registered {} tool(s) across {} server(s)"
            " (yaml={}, db={})",
            len(mcp_tools), len(configs),
            len(yaml_configs), len(configs) - len(yaml_configs),
        )
    else:
        logger.info(
            "[mcp] no servers configured at {}; manager is ready for"
            " runtime attaches via mcp_manage",
            mcp_config_path,
        )

    # single source of truth for attach/detach orchestration.
    # Both the IM tool and the REST handlers delegate here so they
    # cannot diverge on validation, registry hot-add, or persistence.
    mcp_lifecycle = MCPLifecycleService(
        manager=mcp_manager,
        registry=tool_registry,
        store=mcp_store,
    )

    # register the agent-facing attach/detach tool. Confirm
    # tier, so every IM-driven attach still goes through the standard
    # yes/no flow before a subprocess gets spawned.
    # v1.1.0 — workspace_dir threads through so install logs land in
    # workspace/logs/mcp-install.log. Without it the install action
    # still works (logger best-effort guards), just no postmortem file.
    tool_registry.register(MCPManageTool(
        lifecycle=mcp_lifecycle,
        manager=mcp_manager,
        registry=tool_registry,
        store=mcp_store,
        workspace_dir=settings.workspace_dir,
    ))

    return mcp_manager, mcp_store, mcp_lifecycle
