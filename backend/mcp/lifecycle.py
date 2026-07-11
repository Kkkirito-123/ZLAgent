"""Shared attach/detach orchestration for MCP servers (v0.34).

Two surfaces want to attach an MCP server at runtime:

1. The :class:`backend.tools.builtins.mcp_manage.MCPManageTool` — IM-driven,
   permission=confirm, the agent calls it after the operator says "connect
   this server".
2. The REST endpoints ``POST /api/mcp/servers`` and ``DELETE
   /api/mcp/servers/{name}`` — operator-driven, no LLM in the loop, used by
   the future Web Ops Panel and direct curl smoke tests.

Both code paths must execute the same five steps in the same order:

* validate the requested config (name regex, transport mutex, required
  fields by transport),
* spin a connection through :meth:`MCPManager.add_server`,
* register the discovered tool wrappers into :class:`ToolRegistry`,
  honouring per-tool ``tool_override_permission`` promotions,
* persist the runtime row into :class:`MCPServerStore`,
* on remove, undo all four in the reverse order.

This module is the single source of truth. Both call sites delegate to
the service and only differ in how they render the result back to their
respective clients (Markdown text for IM, JSON for REST).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from .config import MCPServerConfig
from .tool_wrapper import (
    MCPTool,
    is_description_safe,
    make_qualified_tool_name,
)
from ..tools.base import ToolPermission


# Same identifier rule the YAML loader and the agent tool already
# enforce; centralised here so REST and tool can't drift.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_VALID_TRANSPORTS = ("stdio", "http")
_VALID_PERMISSIONS = ("safe", "confirm")


# stdio env hardening. When an MCP server starts as a subprocess,
# a handful of env vars get consulted by the interpreter / dynamic linker /
# shell BEFORE the first MCP RPC runs. An LLM that emits a hostile
# mcp_manage call with one of these in the env block can run arbitrary
# code on startup even if the MCP protocol layer is pristine.
#
# Threat classes blocked here:
#   - Node.js:   NODE_OPTIONS, NODE_PATH (runs --require / custom loaders)
#   - Python:    PYTHONSTARTUP, PYTHONPATH, PYTHONHOME, PYTHONBREAKPOINT
#   - Perl/Ruby: PERL5OPT, PERL5LIB, RUBYOPT, RUBYLIB, RUBYSHELL
#   - Shell:     BASH_ENV, ENV, SHELLOPTS, PS4, IFS, PROMPT_COMMAND
#   - Dynamic linker: LD_* (Linux), DYLD_* (macOS), BASH_FUNC_* (bash)
#   - libc:      GCONV_PATH, GLIBC_TUNABLES, HOSTALIASES
#   - JVM:       JAVA_OPTS, JAVA_TOOL_OPTIONS, _JAVA_OPTIONS, JDK_JAVA_OPTIONS
#   - .NET:      DOTNET_STARTUP_HOOKS, DOTNET_ADDITIONAL_DEPS, CORECLR_PROFILER
#   - Build:     MAVEN_OPTS, GRADLE_OPTS, SBT_OPTS, ANT_OPTS, CATALINA_OPTS
#   - Compilers: CC, CXX, RUSTC_WRAPPER, CARGO_BUILD_RUSTC{,_WRAPPER}
#   - Editors/VCS that auto-exec: GIT_EDITOR, GIT_EXTERNAL_DIFF,
#     GIT_SEQUENCE_EDITOR, GIT_SSL_NO_VERIFY, GIT_SSL_CA{INFO,PATH},
#     GIT_TEMPLATE_DIR, SVN_EDITOR, SVN_SSH, BZR_EDITOR, BZR_SSH,
#     BZR_PLUGIN_PATH, SVN_*, SUDO_ASKPASS
#   - Editor init files: EXINIT, VIMINIT, MYVIMRC, GVIMINIT, LUA_INIT,
#     LUA_INIT_5_[1-4], EMACSLOADPATH
#   - Misc:      SHELL, SSLKEYLOGFILE, BROWSER, MAKEFLAGS, MFLAGS, HELM_PLUGINS,
#     PACKER_PLUGIN_PATH, VAGRANT_VAGRANTFILE, ERL_*, ELIXIR_ERL_OPTIONS,
#     R_ENVIRON*, R_PROFILE*, MAKESHELL, CONFIG_SITE, CONFIG_SHELL,
#     CMAKE_TOOLCHAIN_FILE, HGRCPATH, GIT_HOOK_PATH, GIT_DIR, GIT_WORK_TREE,
#     GIT_COMMON_DIR, GIT_EXEC_PATH, GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY,
#     GIT_ALTERNATE_OBJECT_DIRECTORIES, GIT_NAMESPACE, JULIA_EDITOR,
#     CMAKE_C_COMPILER, CMAKE_CXX_COMPILER
#
# Copied verbatim from OpenClaw's `host-env-security-policy.json`
# (`blockedEverywhereKeys` + `blockedPrefixes`) under the MIT License; see
# THIRD_PARTY_NOTICES.md. It's intentionally over-inclusive: false positives
# here are a configuration complaint, false negatives are a security breach.
_BLOCKED_ENV_KEYS: frozenset[str] = frozenset({
    "NODE_OPTIONS", "NODE_PATH",
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONBREAKPOINT",
    "PERL5LIB", "PERL5OPT",
    "RUBYLIB", "RUBYOPT", "RUBYSHELL",
    "BASH_ENV", "ENV", "SHELL", "SHELLOPTS", "PS4", "PROMPT_COMMAND",
    "GCONV_PATH", "IFS", "GLIBC_TUNABLES", "HOSTALIASES",
    "SSLKEYLOGFILE",
    "JAVA_OPTS", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
    "DOTNET_STARTUP_HOOKS", "DOTNET_ADDITIONAL_DEPS",
    "MAVEN_OPTS", "GRADLE_OPTS", "SBT_OPTS", "ANT_OPTS", "CATALINA_OPTS",
    "MAKEFLAGS", "MFLAGS",
    "CC", "CXX", "RUSTC_WRAPPER",
    "CARGO_BUILD_RUSTC", "CARGO_BUILD_RUSTC_WRAPPER",
    "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER", "CMAKE_TOOLCHAIN_FILE",
    "BROWSER",
    "GIT_EDITOR", "GIT_EXTERNAL_DIFF", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_COMMON_DIR", "GIT_EXEC_PATH", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE", "GIT_SEQUENCE_EDITOR", "GIT_TEMPLATE_DIR",
    "GIT_SSL_NO_VERIFY", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
    "GIT_HOOK_PATH",
    "SVN_EDITOR", "SVN_SSH",
    "BZR_EDITOR", "BZR_SSH", "BZR_PLUGIN_PATH",
    "SUDO_ASKPASS", "JULIA_EDITOR",
    "CORECLR_PROFILER",
    "HELM_PLUGINS", "PACKER_PLUGIN_PATH", "VAGRANT_VAGRANTFILE",
    "ERL_AFLAGS", "ERL_FLAGS", "ERL_ZFLAGS", "ELIXIR_ERL_OPTIONS",
    "R_ENVIRON", "R_PROFILE", "R_ENVIRON_USER", "R_PROFILE_USER",
    "CONFIG_SITE", "CONFIG_SHELL",
    "HGRCPATH",
    "EXINIT", "VIMINIT", "MYVIMRC", "GVIMINIT",
    "LUA_INIT", "LUA_INIT_5_1", "LUA_INIT_5_2", "LUA_INIT_5_3", "LUA_INIT_5_4",
    "EMACSLOADPATH",
})

# Prefix-based blocks: any key whose name begins with one of these is
# rejected. Covers the entire dynamic-linker namespace on Linux/macOS
# and bash's exported-function smuggling mechanism.
_BLOCKED_ENV_PREFIXES: tuple[str, ...] = ("LD_", "DYLD_", "BASH_FUNC_")


# -- Public dataclasses -------------------------------------------------------

@dataclass(slots=True)
class AttachOutcome:
    """What ``attach`` produced.

    ``ok=True`` means the server is registered AND (if enabled) connected.
    ``registered`` is the count of agent-facing tool wrappers added to the
    registry. ``skipped`` records each (tool_name, reason) pair that the
    advertised tool list lost during registration — typically schema
    issues or scanner refusals. ``persist_error`` is set if the live
    server is up but writing the SQLite row failed; the caller may want
    to surface this as a soft warning.
    """

    ok: bool
    error: Optional[str] = None
    config: Optional[MCPServerConfig] = None
    registered: int = 0
    qualified_names: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    persist_error: Optional[str] = None


@dataclass(slots=True)
class DetachOutcome:
    """What ``detach`` produced."""

    ok: bool
    error: Optional[str] = None
    unregistered: list[str] = field(default_factory=list)
    manager_dropped: bool = False
    store_deleted: bool = False


@dataclass(slots=True)
class ReconnectOutcome:
    """What ``reconnect`` produced.

    Reconnect rolls the wrappers as well (drops the old set, registers
    whatever the server now advertises). ``dropped`` and ``re_registered``
    let the caller surface the delta.
    """

    ok: bool
    error: Optional[str] = None
    dropped: list[str] = field(default_factory=list)
    re_registered: int = 0
    qualified_names: list[str] = field(default_factory=list)


# -- Argument validation ------------------------------------------------------

class AttachArgError(ValueError):
    """Raised by :func:`validate_attach_args` for any reject reason."""


def validate_attach_args(args: dict[str, Any]) -> MCPServerConfig:
    """Convert a request body / tool-call args dict into a validated config.

    Raises :class:`AttachArgError` on any reject path. Both the REST handler
    and the IM tool funnel through here so error messages are byte-identical
    across surfaces.
    """
    args = args or {}

    name = _require_str(args, "name")
    if not _NAME_RE.match(name):
        raise AttachArgError(
            f"name {name!r} must match [A-Za-z_][A-Za-z0-9_-]{{0,63}}"
        )

    raw_transport = (args.get("transport") or "").strip().lower()
    command = (args.get("command") or "").strip()
    url = (args.get("url") or "").strip()

    # Be forgiving when the LLM omits transport but supplies an obviously
    # corresponding payload. This is the common failure mode for
    # install_and_add / add calls where the model gives us ``command``
    # (stdio) or ``url`` (http) but forgets to echo the transport field.
    # We still reject genuinely ambiguous or incomplete payloads.
    if raw_transport:
        transport = raw_transport
    elif url:
        transport = "http"
    elif command:
        transport = "stdio"
    else:
        raise AttachArgError("missing required argument 'transport'")

    if transport not in _VALID_TRANSPORTS:
        raise AttachArgError(
            f"transport must be one of {list(_VALID_TRANSPORTS)},"
            f" got {transport!r}"
        )

    if transport == "stdio" and not command:
        raise AttachArgError("transport 'stdio' requires non-empty 'command'")
    if transport == "http":
        if not url:
            raise AttachArgError("transport 'http' requires non-empty 'url'")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise AttachArgError("'url' must start with http:// or https://")

    raw_args = args.get("args") or []
    if not isinstance(raw_args, list):
        raise AttachArgError("'args' must be a list of strings")
    arg_tuple = tuple(str(a) for a in raw_args)
    if transport == "http" and arg_tuple:
        raise AttachArgError("'args' is only valid for stdio transport")

    raw_env = args.get("env") or {}
    if not isinstance(raw_env, dict):
        raise AttachArgError("'env' must be a string mapping")
    env = {str(k): str(v) for k, v in raw_env.items()}
    if transport == "http" and env:
        raise AttachArgError("'env' is only valid for stdio transport")

    # reject interpreter-startup / dynamic-linker / shell-init
    # env keys before they can hijack the subprocess's first microsecond.
    # See the _BLOCKED_ENV_KEYS / _BLOCKED_ENV_PREFIXES comment above for
    # the full threat model.
    for key in env:
        upper = key.upper()
        if upper in _BLOCKED_ENV_KEYS:
            raise AttachArgError(
                f"env key {key!r} is blocked: this name is used by the"
                " interpreter/shell/linker to execute code before the"
                " MCP handshake; use the host process environment"
                " (e.g. Docker Compose) instead of passing it through"
                " mcp_manage/attach"
            )
        for prefix in _BLOCKED_ENV_PREFIXES:
            if upper.startswith(prefix):
                raise AttachArgError(
                    f"env key {key!r} is blocked: the {prefix}* namespace"
                    " controls the dynamic linker / exported bash"
                    " functions and can execute code at subprocess start"
                )

    raw_headers = args.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise AttachArgError("'headers' must be a string mapping")
    headers = {str(k): str(v) for k, v in raw_headers.items()}
    if transport == "stdio" and headers:
        raise AttachArgError("'headers' is only valid for http transport")

    description = str(args.get("description") or "").strip()

    enabled_raw = args.get("enabled")
    enabled = True if enabled_raw is None else bool(enabled_raw)

    raw_overrides = args.get("tool_override_permission") or {}
    if not isinstance(raw_overrides, dict):
        raise AttachArgError(
            "'tool_override_permission' must be a {tool_name: 'safe'|'confirm'}"
            " mapping"
        )
    overrides: dict[str, str] = {}
    for tool_name, perm in raw_overrides.items():
        perm_str = str(perm).strip().lower()
        if perm_str not in _VALID_PERMISSIONS:
            raise AttachArgError(
                f"tool_override_permission[{tool_name!r}] must be one of"
                f" {list(_VALID_PERMISSIONS)}, got {perm!r}"
            )
        overrides[str(tool_name)] = perm_str

    return MCPServerConfig(
        name=name,
        command=command,
        args=arg_tuple,
        env=env,
        url=url,
        headers=headers,
        enabled=enabled,
        description=description,
        tool_override_permission=overrides,
    )


def _require_str(args: dict[str, Any], key: str) -> str:
    raw = args.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise AttachArgError(f"missing required argument '{key}'")
    return raw.strip()


# -- Service ------------------------------------------------------------------

class MCPLifecycleService:
    """Orchestrates the manager + tool registry + store on attach/detach.

    Stateless aside from the three injected references; safe to share
    across the IM tool and the REST handler. The injected references are
    created exactly once at app startup (``app.state.mcp_*``) so this
    class is effectively a process-wide singleton.
    """

    def __init__(self, manager, registry, store) -> None:
        self._manager = manager
        self._registry = registry
        self._store = store

    # -- attach ---------------------------------------------------------------

    async def attach(
        self,
        cfg: MCPServerConfig,
        *,
        source: str = "im",
        created_by: Optional[str] = None,
    ) -> AttachOutcome:
        """Atomically attach a server. See :class:`AttachOutcome`."""
        # Pre-flight: scan the operator-supplied description through the
        # same prompt-injection scanner the per-tool description scan
        # uses. Operator descriptions end up in the system prompt every
        # turn, so a hostile one is just as dangerous as a hostile tool
        # description.
        if cfg.description and not is_description_safe(cfg.description):
            return AttachOutcome(
                ok=False,
                error=(
                    "server 'description' tripped the prompt-injection"
                    " scanner; rewrite it as a plain ASCII summary"
                ),
                config=cfg,
            )

        ok, err, discovered = await self._manager.add_server(cfg)
        if not ok:
            return AttachOutcome(
                ok=False,
                error=err or "add_server failed for unknown reason",
                config=cfg,
            )

        # Hot-register only the freshly-discovered wrappers — re-walking
        # every server here would risk a duplicate-name registration when
        # multiple servers share a tool name (rare, but the wrapper takes
        # care of namespacing).
        registered = 0
        skipped: list[tuple[str, str]] = []
        qualified: list[str] = []
        for descriptor in discovered:
            if not cfg.is_tool_allowed(descriptor.name):
                skipped.append((descriptor.name, "filtered by include/exclude"))
                continue
            if not is_description_safe(descriptor.description):
                skipped.append((descriptor.name, "description scanner refused"))
                continue
            perm_str = cfg.permission_for(descriptor.name)
            permission = (
                ToolPermission.SAFE if perm_str == "safe"
                else ToolPermission.CONFIRM
            )
            try:
                wrapper = MCPTool(
                    self._manager, descriptor, permission=permission,
                )
                self._registry.register(wrapper)
                registered += 1
                qualified.append(wrapper.name)
            except ValueError as exc:
                skipped.append((descriptor.name, f"register: {exc}"))

        # Persist last so a registry/manager rollback isn't needed if the
        # DB write fails — at this point the server is live, persisting
        # is the cheap recoverable bit. We surface the error but stay
        # ``ok=True`` because the live server is what the caller asked
        # for.
        persist_error: Optional[str] = None
        try:
            self._store.upsert(cfg, source=source, created_by=created_by)
        except Exception as exc:  # noqa: BLE001 — best-effort persistence
            logger.warning(
                "[mcp.lifecycle] persisted upsert failed for {!r}: {}",
                cfg.name, exc,
            )
            persist_error = f"{type(exc).__name__}: {exc}"

        logger.info(
            "[mcp.lifecycle] attach '{}' OK (source={}, {} tool(s),"
            " {} skipped, persist_error={})",
            cfg.name, source, registered, len(skipped),
            persist_error or "none",
        )

        return AttachOutcome(
            ok=True,
            error=None,
            config=cfg,
            registered=registered,
            qualified_names=qualified,
            skipped=skipped,
            persist_error=persist_error,
        )

    # -- detach ---------------------------------------------------------------

    async def detach(self, name: str) -> DetachOutcome:
        """Atomically detach a server. See :class:`DetachOutcome`.

        Returns ``ok=False`` if neither the manager nor the store has any
        record of the name. Idempotent against concurrent calls because
        each underlying step (registry / manager / store) is itself
        idempotent on missing names.
        """
        if not isinstance(name, str) or not name.strip():
            return DetachOutcome(ok=False, error="missing required argument 'name'")
        name = name.strip()

        existed_in_manager = self._manager.get_config(name) is not None
        existed_in_store = self._store.get(name) is not None

        # Drop tools first so an in-flight agent turn can't pick a stale
        # wrapper while the connection is mid-teardown.
        full_prefix = make_qualified_tool_name(name, "")
        unregistered = self._registry.unregister_prefix(full_prefix)

        manager_dropped = await self._manager.remove_server(name)
        store_deleted = self._store.delete(name)

        if not (existed_in_manager or existed_in_store):
            return DetachOutcome(
                ok=False,
                error=f"unknown MCP server '{name}'",
                unregistered=unregistered,
                manager_dropped=False,
                store_deleted=False,
            )

        logger.info(
            "[mcp.lifecycle] detach '{}' OK (unregistered={},"
            " manager_dropped={}, store_deleted={})",
            name, len(unregistered), manager_dropped, store_deleted,
        )

        return DetachOutcome(
            ok=True,
            unregistered=unregistered,
            manager_dropped=manager_dropped,
            store_deleted=store_deleted,
        )

    # -- reconnect ------------------------------------------------------------

    async def reconnect(self, name: str) -> ReconnectOutcome:
        """Bounce a server's connection AND refresh its tool wrappers.

        v0.14's REST handler called ``MCPManager.reconnect`` directly,
        which rebooted the connection but left the now-stale wrappers in
        the registry — the LLM would still see the pre-reconnect tool
        list even if the server's catalog had changed. Routing through
        the lifecycle service fixes that: drop the wrappers first, hit
        the manager, register the post-reconnect catalog.
        """
        if not isinstance(name, str) or not name.strip():
            return ReconnectOutcome(ok=False, error="missing required argument 'name'")
        name = name.strip()

        cfg = self._manager.get_config(name)
        if cfg is None:
            return ReconnectOutcome(
                ok=False, error=f"unknown MCP server '{name}'",
            )

        # Drop wrappers first so an in-flight agent turn can't pick a
        # stale one mid-bounce.
        full_prefix = make_qualified_tool_name(name, "")
        dropped = self._registry.unregister_prefix(full_prefix)

        ok = await self._manager.reconnect(name)
        if not ok:
            conn = self._manager.get_connection(name)
            err = (conn.ready_error if conn is not None else None) or (
                "reconnect failed"
            )
            return ReconnectOutcome(
                ok=False, error=f"reconnect '{name}': {err}",
                dropped=dropped,
            )

        # Re-register from whatever the server now advertises.
        re_registered = 0
        qualified: list[str] = []
        conn = self._manager.get_connection(name)
        if conn is not None:
            for descriptor in conn.tools:
                if not cfg.is_tool_allowed(descriptor.name):
                    continue
                if not is_description_safe(descriptor.description):
                    continue
                perm_str = cfg.permission_for(descriptor.name)
                permission = (
                    ToolPermission.SAFE if perm_str == "safe"
                    else ToolPermission.CONFIRM
                )
                try:
                    wrapper = MCPTool(
                        self._manager, descriptor, permission=permission,
                    )
                    self._registry.register(wrapper)
                    re_registered += 1
                    qualified.append(wrapper.name)
                except ValueError:
                    pass

        logger.info(
            "[mcp.lifecycle] reconnect '{}' OK (dropped={}, re_registered={})",
            name, len(dropped), re_registered,
        )
        return ReconnectOutcome(
            ok=True, dropped=dropped, re_registered=re_registered,
            qualified_names=qualified,
        )


__all__ = [
    "AttachArgError",
    "AttachOutcome",
    "DetachOutcome",
    "ReconnectOutcome",
    "MCPLifecycleService",
    "validate_attach_args",
]
