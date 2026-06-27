"""REST surface for the v0.14 / v0.34 / v1.1.0 MCP subsystem.

Endpoints:

* ``GET    /api/mcp/servers``                  — list configured servers + status
* ``POST   /api/mcp/servers``                  — v0.34: attach a new server
* ``GET    /api/mcp/servers/{name}``           — single server status
* ``DELETE /api/mcp/servers/{name}``           — v0.34: detach a server
* ``POST   /api/mcp/servers/{name}/reconnect`` — tear down + reopen
* ``GET    /api/mcp/tools``                    — discovered tools across all
                                                 connected servers (for ops UI)
* ``POST   /api/mcp/install``                  — v1.1.0: install a package
* ``GET    /api/mcp/installed``                — v1.1.0: list installed packages

v0.34 promoted these from read-only to a full management surface: the
``POST`` and ``DELETE`` routes execute the same attach/detach orchestration
the IM-driven ``mcp_manage`` tool runs, just bypassing the confirmation
flow because the operator is acting on the API directly. Both routes
delegate to :class:`MCPLifecycleService` so the agent and the REST API
can never disagree about what attaching a server actually does. Tool
*invocation* still goes through the agent loop, which honours the
per-tool permission tier.

v1.1.0 adds the install lifecycle: ``POST /install`` calls the same
:func:`backend.mcp.installer.install_package` the IM tool drives, so
curl-driven setup scripts and IM-driven installs cannot diverge on
validation or sandboxing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..mcp.installer import (
    InstallArgError,
    install_package,
    list_installed,
    validate_install_args,
)
from ..mcp.lifecycle import (
    AttachArgError,
    MCPLifecycleService,
    validate_attach_args,
)
from ..mcp.manager import MCPManager, ServerStatus

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _get_manager(request: Request) -> Optional[MCPManager]:
    return getattr(request.app.state, "mcp_manager", None)


def _get_lifecycle(request: Request) -> Optional[MCPLifecycleService]:
    return getattr(request.app.state, "mcp_lifecycle", None)


# -- Schemas ------------------------------------------------------------------

class ServerStatusOut(BaseModel):
    name: str
    enabled: bool
    connected: bool
    error: Optional[str] = None
    tool_count: int
    breaker_open: bool
    consecutive_failures: int
    description: str = ""
    transport: str = "stdio"

    @classmethod
    def from_status(cls, s: ServerStatus) -> "ServerStatusOut":
        return cls(
            name=s.name,
            enabled=s.enabled,
            connected=s.connected,
            error=s.error,
            tool_count=s.tool_count,
            breaker_open=s.breaker_open,
            consecutive_failures=s.consecutive_failures,
            description=s.description,
            transport=s.transport,
        )


class ServerListResponse(BaseModel):
    count: int
    connected: int
    servers: list[ServerStatusOut]


class DiscoveredToolOut(BaseModel):
    server: str
    name: str
    qualified_name: str = Field(
        description="Tool name as the agent sees it (mcp__<server>__<tool>).",
    )
    description: str
    input_schema: dict[str, Any]


class ToolListResponse(BaseModel):
    count: int
    tools: list[DiscoveredToolOut]


class ReconnectResponse(BaseModel):
    ok: bool
    server: str
    error: Optional[str] = None
    dropped: int = 0
    re_registered: int = 0


# attach/detach schemas. The attach body intentionally mirrors the
# ``mcp_manage(action='add', ...)`` arguments so docs and operator muscle
# memory transfer 1:1 between the IM tool and curl.

class AttachServerRequest(BaseModel):
    name: str = Field(
        ...,
        description=(
            "Server name. Becomes the mcp__<name>__<tool> prefix on every"
            " advertised tool. Must match [A-Za-z_][A-Za-z0-9_-]{0,63}."
        ),
    )
    transport: str = Field(
        ...,
        description="'stdio' (subprocess) or 'http' (streamable-HTTP).",
    )
    command: str = Field(
        default="",
        description="stdio only — executable to launch.",
    )
    args: list[str] = Field(
        default_factory=list,
        description="stdio only — argv passed verbatim.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="stdio only — extra environment variables.",
    )
    url: str = Field(
        default="",
        description="http only — endpoint URL (must start with http:// or https://).",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="http only — extra request headers.",
    )
    description: str = Field(
        default="",
        description="Operator-facing summary.",
    )
    enabled: bool = Field(
        default=True,
        description="Set false to register without starting a connection.",
    )
    tool_override_permission: dict[str, str] = Field(
        default_factory=dict,
        description="Per-tool 'safe'|'confirm' overrides.",
    )


class AttachServerResponse(BaseModel):
    ok: bool
    name: str
    error: Optional[str] = None
    registered: int = 0
    qualified_names: list[str] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)
    persist_error: Optional[str] = None


class DetachServerResponse(BaseModel):
    ok: bool
    name: str
    error: Optional[str] = None
    unregistered: list[str] = Field(default_factory=list)
    manager_dropped: bool = False
    store_deleted: bool = False


# -- Routes -------------------------------------------------------------------

@router.get("/servers", response_model=ServerListResponse)
def list_servers(request: Request) -> ServerListResponse:
    manager = _get_manager(request)
    if manager is None:
        # MCP can be entirely disabled — return an empty list so the ops UI
        # doesn't error out, just shows "no servers".
        return ServerListResponse(count=0, connected=0, servers=[])
    statuses = manager.status()
    connected = sum(1 for s in statuses if s.connected)
    return ServerListResponse(
        count=len(statuses),
        connected=connected,
        servers=[ServerStatusOut.from_status(s) for s in statuses],
    )


@router.get("/servers/{name}", response_model=ServerStatusOut)
def get_server(name: str, request: Request) -> ServerStatusOut:
    manager = _get_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="MCP subsystem disabled")
    for s in manager.status():
        if s.name == name:
            return ServerStatusOut.from_status(s)
    raise HTTPException(status_code=404, detail=f"unknown MCP server '{name}'")


@router.post("/servers", response_model=AttachServerResponse, status_code=201)
async def attach_server(
    payload: AttachServerRequest, request: Request,
) -> AttachServerResponse:
    """Attach a new MCP server at runtime (v0.34).

    Equivalent to the IM-driven ``mcp_manage(action='add', ...)`` minus
    the confirmation prompt. The lifecycle service handles validation,
    spawning the connection, registering tool wrappers, and persisting
    the runtime row to SQLite. ``201 Created`` on success; ``400`` for
    a bad payload (validation reject); ``409`` if a server with that
    name already exists; ``503`` if the MCP subsystem is disabled.
    """
    lifecycle = _get_lifecycle(request)
    if lifecycle is None:
        raise HTTPException(status_code=503, detail="MCP subsystem disabled")
    try:
        cfg = validate_attach_args(payload.model_dump())
    except AttachArgError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    manager = _get_manager(request)
    if manager is not None and manager.get_config(cfg.name) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"MCP server '{cfg.name}' is already registered",
        )

    outcome = await lifecycle.attach(
        cfg, source="rest", created_by=None,
    )
    if not outcome.ok:
        # Differentiate validation-class errors (400) from runtime errors
        # (502 — the upstream MCP server / subprocess failed to start).
        msg = (outcome.error or "").lower()
        if "tripped" in msg or "must " in msg or "required" in msg:
            raise HTTPException(status_code=400, detail=outcome.error)
        raise HTTPException(status_code=502, detail=outcome.error)
    return AttachServerResponse(
        ok=True,
        name=cfg.name,
        registered=outcome.registered,
        qualified_names=outcome.qualified_names,
        skipped=[{"tool": t, "reason": r} for (t, r) in outcome.skipped],
        persist_error=outcome.persist_error,
    )


@router.delete("/servers/{name}", response_model=DetachServerResponse)
async def detach_server(name: str, request: Request) -> DetachServerResponse:
    """Detach a server (v0.34). Returns 200 with ok=true on success,
    404 if the name doesn't exist anywhere, 503 if MCP is disabled."""
    lifecycle = _get_lifecycle(request)
    if lifecycle is None:
        raise HTTPException(status_code=503, detail="MCP subsystem disabled")
    outcome = await lifecycle.detach(name)
    if not outcome.ok:
        # Lifecycle returns ok=False with "unknown" for missing names; map
        # that to 404 so the REST contract is conventional. Other failures
        # would be programming errors at this layer (none currently
        # reachable), so default to 500.
        if (outcome.error or "").lower().startswith("unknown"):
            raise HTTPException(status_code=404, detail=outcome.error)
        raise HTTPException(status_code=500, detail=outcome.error or "detach failed")
    return DetachServerResponse(
        ok=True,
        name=name,
        unregistered=outcome.unregistered,
        manager_dropped=outcome.manager_dropped,
        store_deleted=outcome.store_deleted,
    )


@router.post("/servers/{name}/reconnect", response_model=ReconnectResponse)
async def reconnect_server(name: str, request: Request) -> ReconnectResponse:
    """Bounce a server's connection (v0.34: now goes through lifecycle so
    the tool registry is refreshed if the server's catalog changed)."""
    lifecycle = _get_lifecycle(request)
    if lifecycle is None:
        raise HTTPException(status_code=503, detail="MCP subsystem disabled")
    manager = _get_manager(request)
    if manager is None or manager.get_config(name) is None:
        raise HTTPException(status_code=404, detail=f"unknown MCP server '{name}'")
    outcome = await lifecycle.reconnect(name)
    return ReconnectResponse(
        ok=outcome.ok,
        server=name,
        error=outcome.error,
        dropped=len(outcome.dropped),
        re_registered=outcome.re_registered,
    )


@router.get("/tools", response_model=ToolListResponse)
def list_tools(request: Request) -> ToolListResponse:
    manager = _get_manager(request)
    if manager is None:
        return ToolListResponse(count=0, tools=[])
    from ..mcp.tool_wrapper import make_qualified_tool_name

    out: list[DiscoveredToolOut] = []
    for descriptor in manager.list_tools():
        out.append(
            DiscoveredToolOut(
                server=descriptor.server_name,
                name=descriptor.name,
                qualified_name=make_qualified_tool_name(
                    descriptor.server_name, descriptor.name,
                ),
                description=descriptor.description,
                input_schema=descriptor.input_schema,
            )
        )
    return ToolListResponse(count=len(out), tools=out)


# -- v1.1.0 install routes ----------------------------------------------------

class InstallPackageRequest(BaseModel):
    """Mirror of mcp_manage(action='install') args for the REST surface."""

    package_manager: str = Field(
        ...,
        description="One of: npm, pip, uvx, git_npm, git_pip.",
    )
    package: str = Field(
        ...,
        description=(
            "Registry name ('amap-mcp-server', '@org/foo', 'foo@1.2.3')"
            " or git URL ('https://github.com/...' or 'git+https://...')"
            " for the git_* managers."
        ),
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description="Whitelisted extra flags; safety flags cannot be overridden.",
    )
    timeout_seconds: float = Field(
        default=180.0,
        description="Kill the subprocess after this many seconds (max 600).",
    )
    allow_scripts: bool = Field(
        default=False,
        description=(
            "Opt out of --ignore-scripts. Leave false unless the package"
            " documentation explicitly requires postinstall."
        ),
    )


class InstallPackageResponse(BaseModel):
    ok: bool
    package_manager: str
    package: str
    command: list[str]
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: int = 0
    exit_code: Optional[int] = None
    error: Optional[str] = None


class ListedPackageOut(BaseModel):
    package_manager: str
    name: str
    version: str = ""
    location: str = ""


class InstalledListResponse(BaseModel):
    ok: bool
    package_manager: str
    count: int
    packages: list[ListedPackageOut] = Field(default_factory=list)
    error: Optional[str] = None


def _get_workspace_dir(request: Request) -> Optional[Path]:
    """Resolve workspace_dir from app.state so install logs land there."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return None
    raw = getattr(settings, "workspace_dir", None)
    if raw is None:
        return None
    return Path(raw)


@router.post(
    "/install", response_model=InstallPackageResponse, status_code=200,
)
async def install_package_route(
    payload: InstallPackageRequest, request: Request,
) -> InstallPackageResponse:
    """Install a package binary into the managed prefix.

    Bypasses the IM yes/no since the caller is hitting REST directly
    (operator-blessed action). Validation, sandboxing, and logging are
    identical to the IM path because both go through
    :func:`backend.mcp.installer.install_package`.

    Returns 200 with ``ok=false`` on subprocess failure (the body
    carries the stderr tail). Returns 400 on validation reject.
    """
    try:
        req = validate_install_args(payload.model_dump())
    except InstallArgError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    workspace_dir = _get_workspace_dir(request)
    outcome = await install_package(req, workspace_dir=workspace_dir)
    return InstallPackageResponse(
        ok=outcome.ok,
        package_manager=outcome.package_manager,
        package=outcome.package,
        command=list(outcome.command),
        stdout_tail=outcome.stdout_tail,
        stderr_tail=outcome.stderr_tail,
        duration_ms=outcome.duration_ms,
        exit_code=outcome.exit_code,
        error=outcome.error,
    )


class InstallAndAddRequest(InstallPackageRequest, AttachServerRequest):
    """Union of install + attach args (v1.1.0+).

    Pydantic v2 lets us multiply-inherit from both schemas; field names
    are disjoint between the install side and the attach side so there
    are no collisions. The route delegates to
    :meth:`MCPLifecycleService.attach` + the installer module, matching
    the IM tool's ``install_and_add`` action.
    """


class InstallAndAddResponse(BaseModel):
    ok: bool
    stage: str  # "validated" | "installed" | "attached"
    install: InstallPackageResponse
    attach: Optional[AttachServerResponse] = None
    error: Optional[str] = None


@router.post(
    "/install_and_add",
    response_model=InstallAndAddResponse,
    status_code=200,
)
async def install_and_add_route(
    payload: InstallAndAddRequest, request: Request,
) -> InstallAndAddResponse:
    """Atomic install + attach for runtime MCP onboarding.

    Equivalent to ``mcp_manage(action='install_and_add', ...)`` minus
    the IM yes/no. Validation runs both halves up-front. Returns 200
    in all post-validation outcomes so the caller can inspect the
    ``stage`` field; 400 only on argument validation.
    """
    payload_dict = payload.model_dump()
    try:
        install_req = validate_install_args(payload_dict)
    except InstallArgError as exc:
        raise HTTPException(
            status_code=400, detail=f"install args: {exc}",
        ) from exc
    try:
        attach_cfg = validate_attach_args(payload_dict)
    except AttachArgError as exc:
        raise HTTPException(
            status_code=400, detail=f"attach args: {exc}",
        ) from exc

    lifecycle = _get_lifecycle(request)
    workspace_dir = _get_workspace_dir(request)

    install_outcome = await install_package(
        install_req, workspace_dir=workspace_dir,
    )
    install_resp = InstallPackageResponse(
        ok=install_outcome.ok,
        package_manager=install_outcome.package_manager,
        package=install_outcome.package,
        command=list(install_outcome.command),
        stdout_tail=install_outcome.stdout_tail,
        stderr_tail=install_outcome.stderr_tail,
        duration_ms=install_outcome.duration_ms,
        exit_code=install_outcome.exit_code,
        error=install_outcome.error,
    )
    if not install_outcome.ok:
        return InstallAndAddResponse(
            ok=False, stage="installed", install=install_resp,
            error=install_outcome.error or "install failed",
        )

    if lifecycle is None:
        return InstallAndAddResponse(
            ok=False, stage="installed", install=install_resp,
            error="MCP subsystem disabled; package installed",
        )

    manager = _get_manager(request)
    if manager is not None and manager.get_config(attach_cfg.name) is not None:
        return InstallAndAddResponse(
            ok=False, stage="installed", install=install_resp,
            error=(
                f"MCP server '{attach_cfg.name}' is already registered;"
                " the package was installed but not re-attached. Use"
                " mcp_manage(action='reconnect', ...) if you wanted a"
                " bounce."
            ),
        )

    attach_outcome = await lifecycle.attach(
        attach_cfg, source="rest", created_by=None,
    )
    attach_resp = AttachServerResponse(
        ok=attach_outcome.ok,
        name=attach_cfg.name,
        registered=attach_outcome.registered,
        qualified_names=attach_outcome.qualified_names,
        skipped=[
            {"tool": t, "reason": r} for (t, r) in attach_outcome.skipped
        ],
        persist_error=attach_outcome.persist_error,
        error=attach_outcome.error,
    )
    return InstallAndAddResponse(
        ok=attach_outcome.ok,
        stage="attached",
        install=install_resp,
        attach=attach_resp,
        error=None if attach_outcome.ok else (
            attach_outcome.error or "attach failed after install"
        ),
    )


@router.get(
    "/installed", response_model=InstalledListResponse,
)
async def installed_packages_route(
    package_manager: str, request: Request,
) -> InstalledListResponse:
    """List packages already installed in the managed prefix.

    Query param ``package_manager`` is mandatory and must be one of
    npm / pip / uvx; the git_* managers route through whichever
    underlying manager actually installed them.
    """
    outcome = await list_installed(package_manager)
    return InstalledListResponse(
        ok=outcome.ok,
        package_manager=outcome.package_manager,
        count=len(outcome.packages),
        packages=[
            ListedPackageOut(
                package_manager=p.package_manager,
                name=p.name, version=p.version, location=p.location,
            )
            for p in outcome.packages
        ],
        error=outcome.error,
    )
