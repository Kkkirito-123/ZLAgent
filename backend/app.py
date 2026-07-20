"""FastAPI entry point for ZLAgent."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from . import __version__
from .core.config import get_settings
from .core.logging import configure_logging
from .cron.runner_adapter import build_cron_runtime_adapter
from .db.models import apply_lightweight_migrations, create_all
from .db.session import engine
from .bootstrap.gateways import build_gateway_manager
from .bootstrap.message_dispatch import GatewayTurnDispatcher
from .bootstrap.runtime import (
    attach_gateway_tools,
    bind_runtime_state,
    build_runtime_container,
    load_runtime_plugins,
)
from .bootstrap.services import (
    bind_service_state,
    build_runtime_services,
    start_runtime_services,
    stop_runtime_services,
)
from .bootstrap.routes import mount_api_routes


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    settings.ensure_directories()

    logger.info("starting {} v{}", settings.app_name, __version__)
    logger.info("data_dir={} config_dir={} workspace_dir={}",
                settings.data_dir, settings.config_dir, settings.workspace_dir)

    create_all(engine)
    apply_lightweight_migrations(engine)

    # v0.37.8 / optional Redis hot layer in front of session
    # context, travel_realtime bundle cache, and the wiki answer cache.
    # Helper handles the best-effort health check; ``None`` is the
    # disabled state every consumer falls back from.
    from .storage.bootstrap import build_redis_backend_with_healthcheck
    redis_backend = build_redis_backend_with_healthcheck(settings)

    runtime = await build_runtime_container(
        app=app,
        settings=settings,
        redis_backend=redis_backend,
    )
    bind_runtime_state(app, runtime)
    confirmation_store = runtime.confirmation_store
    agent = runtime.agent
    memory_manager = runtime.memory_manager
    mcp_manager = runtime.mcp_manager

    dispatcher = GatewayTurnDispatcher(runtime)

    manager = build_gateway_manager(
        app=app,
        settings=settings,
        agent_handler=dispatcher.route_message,
    )
    dispatcher.bind_gateway_manager(manager)

    # send_message_tool needs the gateway manager to dispatch
    # outbound. Register it after the manager is fully wired up so the
    # tool's confirm-tier flow always finds a real adapter to push
    # through (rather than crashing at execute() time).
    attach_gateway_tools(runtime, manager)

    cron_runtime = build_cron_runtime_adapter(
        settings=settings,
        agent=agent,
        gateway_manager=manager,
    )

    async def _expire_pending_confirmations() -> None:
        # Wrap the sync expire_old call so the scheduler can await it; doing
        # work in a thread is unnecessary for our SQLite scale.
        confirmation_store.expire_old()

    services = await build_runtime_services(
        settings=settings,
        runtime=runtime,
        gateway_manager=manager,
        cron_runner=cron_runtime.run_job,
        expire_pending_confirmations=_expire_pending_confirmations,
    )

    app.state.gateway_manager = manager
    app.state.cron_runner = cron_runtime.run_job
    app.state.cron_deliverer = cron_runtime.deliverer
    bind_service_state(app, services)
    load_runtime_plugins(runtime)
    bind_runtime_state(app, runtime)

    _harness_boot_inventory = app.state.harness.inventory()
    logger.info(
        "harness ready: skills={} mcps_runtime={} plugins={} tools_user={}"
        " (core_hidden: skills={} mcps_yaml={} tools={})",
        len(_harness_boot_inventory.skills),
        len(_harness_boot_inventory.mcps),
        len(_harness_boot_inventory.plugins),
        len(_harness_boot_inventory.tools),
        _harness_boot_inventory.core_summary.get("skills_hidden", 0),
        _harness_boot_inventory.core_summary.get("mcps_hidden", 0),
        _harness_boot_inventory.core_summary.get("tools_hidden", 0),
    )

    await manager.start()
    await start_runtime_services(services)

    try:
        yield
    finally:
        logger.info("shutting down {} v{}", settings.app_name, __version__)
        await dispatcher.stop()
        await stop_runtime_services(services)
        await manager.stop()
        if mcp_manager is not None:
            try:
                await mcp_manager.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[mcp] shutdown error: {}", exc)
        try:
            memory_manager.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[memory] shutdown error: {}", exc)
        # close the Redis pool and unwire the bundle cache so
        # tests / a soft-restart cleanly drop the shared client.
        try:
            from .domains.travel.realtime import (
                set_bundle_cache_redis_backend as _set_bundle_redis,
            )
            _set_bundle_redis(None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[redis] unwire bundle cache failed: {}", exc)
        if redis_backend is not None:
            try:
                redis_backend.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[redis] close failed: {}", exc)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    # Permissive CORS for single-user / local-only deployments where the API
    # is reached from arbitrary local clients. v0.27 will tighten this with
    # an allowlist.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    mount_api_routes(app)
    return app


app = create_app()
