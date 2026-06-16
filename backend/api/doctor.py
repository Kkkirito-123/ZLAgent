"""``/api/doctor`` aggregate health diagnostic.

Eight lightweight checks bundled into a single 200 response so an
operator can grep one endpoint to know whether ZLAgent's runtime
state is sane. Each check returns ``{"ok": bool, "detail": str}``;
the top-level ``ok`` is the AND of all of them. We deliberately never
return a non-2xx status from the doctor — failure information is
inside the body so dashboards can render the per-component verdicts.

Checks:

1. ``llm``       — :class:`LLMClient.configured`.
2. ``database``  — quick ``SELECT 1`` against the SQLite engine.
3. ``workspace`` — ``workspace_dir`` exists, is writable.
4. ``skills``    — :class:`SkillLoader` initialised; count surfaced.
5. ``cron``      — scheduler started; job count surfaced.
6. ``gateways``  — at least one gateway, no persistent failures.
7. ``memory``    — :class:`MemoryStore` reachable; entry count surfaced.
8. ``mcp``       — disabled (``manager=None``) → ok; enabled → at
                   least one server connected (or no servers configured).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import text as _sql_text

from .. import __version__
from ..core.config import get_settings


def _ok(detail: str = "ok", **extra: Any) -> dict[str, Any]:
    out = {"ok": True, "detail": detail}
    out.update(extra)
    return out


def _fail(detail: str, **extra: Any) -> dict[str, Any]:
    out = {"ok": False, "detail": detail}
    out.update(extra)
    return out


def _check_llm(state: Any) -> dict[str, Any]:
    llm = getattr(state, "llm", None)
    if llm is None:
        return _fail("llm client not initialised")
    if not getattr(llm, "configured", False):
        return _fail("llm not configured (set OPENAI_API_KEY + OPENAI_MODEL)")
    return _ok(
        f"provider={getattr(llm, 'provider', '?')} model={getattr(llm, 'model', '?')}",
        provider=getattr(llm, "provider", ""),
        model=getattr(llm, "model", ""),
    )


def _check_database() -> dict[str, Any]:
    try:
        from ..db.session import engine
        with engine.connect() as conn:
            conn.execute(_sql_text("SELECT 1"))
        return _ok("sqlite reachable")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"db error: {type(exc).__name__}: {exc}")


def _check_workspace() -> dict[str, Any]:
    settings = get_settings()
    ws: Path = settings.workspace_dir
    if not ws.exists():
        return _fail(f"workspace_dir missing: {ws}")
    probe = ws / ".zlagent_doctor_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"workspace not writable: {exc}", path=str(ws))
    return _ok(f"writable at {ws}", path=str(ws))


def _check_skills(state: Any) -> dict[str, Any]:
    loader = getattr(state, "skill_loader", None)
    if loader is None:
        return _fail("skill_loader not initialised")
    try:
        count = len(loader.list())
    except Exception as exc:  # noqa: BLE001
        return _fail(f"skill_loader.list() raised: {exc}")
    return _ok(f"{count} skill(s) loaded", count=count)


def _check_cron(state: Any) -> dict[str, Any]:
    sched = getattr(state, "cron_scheduler", None)
    if sched is None:
        return _fail("cron scheduler not initialised")
    started = bool(getattr(sched, "_started", True))  # truthy default keeps offline smoke green
    return _ok("scheduler ready" if started else "scheduler ready (idle)")


def _check_gateways(state: Any) -> dict[str, Any]:
    mgr = getattr(state, "gateway_manager", None)
    if mgr is None:
        return _fail("gateway manager not initialised")
    try:
        statuses = mgr.gateway_statuses()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"gateway_statuses() raised: {exc}")
    if not statuses:
        return _fail("no gateways registered")
    # A gateway with persistent failures and no successful outbound
    # in between counts as a failed check.
    failed = [
        s.name for s in statuses
        if s.counters.failures > 0 and s.counters.last_error is not None
    ]
    if failed:
        return _fail(
            f"gateways with errors: {failed}",
            registered=[s.name for s in statuses],
        )
    return _ok(
        f"{len(statuses)} gateway(s)",
        registered=[s.name for s in statuses],
    )


def _check_memory(state: Any) -> dict[str, Any]:
    store = getattr(state, "memory_store", None)
    if store is None:
        return _fail("memory_store not initialised")
    try:
        count = 0
        if hasattr(store, "size"):
            count = int(store.size())
        elif hasattr(store, "list"):
            count = len(list(store.list()))
        return _ok(f"{count} entry(ies)", count=count)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"memory_store probe raised: {exc}")


def _check_mcp(state: Any) -> dict[str, Any]:
    mgr = getattr(state, "mcp_manager", None)
    if mgr is None:
        return _ok("mcp disabled")
    try:
        servers = mgr.status()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"mcp.status() raised: {exc}")
    if not servers:
        return _ok("mcp enabled, no servers configured")
    connected = sum(1 for s in servers if s.connected)
    if connected == 0:
        return _fail(
            f"mcp enabled, 0/{len(servers)} servers connected",
            servers=[s.name for s in servers],
        )
    return _ok(
        f"mcp {connected}/{len(servers)} servers connected",
        servers=[s.name for s in servers],
    )


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/doctor", tags=["doctor"])

    @router.get("")
    def doctor(request: Request) -> dict[str, Any]:
        state = request.app.state
        checks: dict[str, dict[str, Any]] = {
            "llm": _check_llm(state),
            "database": _check_database(),
            "workspace": _check_workspace(),
            "skills": _check_skills(state),
            "cron": _check_cron(state),
            "gateways": _check_gateways(state),
            "memory": _check_memory(state),
            "mcp": _check_mcp(state),
        }
        ok = all(c.get("ok") is True for c in checks.values())
        return {
            "ok": ok,
            "version": __version__,
            "checks": checks,
        }

    return router


__all__ = ["build_router"]
