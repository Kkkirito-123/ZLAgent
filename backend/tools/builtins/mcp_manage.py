"""mcp_manage: agent-managed MCP server attachment (confirm tier, v0.33+).

This is the IM-facing entry point that turns ZLAgent into a real plugin
platform: any MCP server reachable from the host can be attached at
runtime without editing YAML or restarting. The tool wraps four layers:

1. :class:`backend.mcp.installer` (v1.1.0) — install the package
   binary itself (``npm install -g`` / ``pip install --user`` /
   ``uv tool install``) so a stdio command can resolve on PATH.
2. :class:`backend.mcp.manager.MCPManager.add_server` /
   :meth:`remove_server` — manage in-process connection lifecycle.
3. :class:`backend.tools.registry.ToolRegistry` — register / unregister
   the discovered tool wrappers so the agent can immediately call them
   on the next turn.
4. :class:`backend.mcp.store.MCPServerStore` — persist the runtime
   registration into SQLite so the new server survives a restart.

Read-only actions (``list``, ``inspect``, ``installed``) are exempt
from the confirm prompt via :meth:`is_action_read_only`. Mutating
actions (``add``, ``remove``, ``reconnect``, ``install``, ``promote``)
all go through ``permission=confirm`` because they change process
state (spawning subprocesses, opening sockets, downloading code from
public registries, exposing new tools to the LLM).

Actions:

* ``list``       no args — summary of every registered server.
* ``add``        name + transport + (command|url) — attach a new server.
* ``remove``     name — detach a runtime-registered server.
* ``reconnect``  name — bounce one server's connection (re-discover tools).
* ``inspect``    name — full status + advertised tools for one server.
* ``install``    package_manager + package — fetch the binary so a
                 follow-up ``add`` finds it on PATH (v1.1.0).
* ``installed``  package_manager — list packages already installed
                 in the managed prefix (v1.1.0, read-only).
* ``promote``    name + (tool_name | tool_pattern) + permission —
                 flip individual tools' permission tier so query-only
                 servers stop asking confirm on every call (v1.1.0).

Naming uses the existing ``mcp__<server>__<tool>`` convention enforced by
:func:`backend.mcp.tool_wrapper.make_qualified_tool_name`.
"""
from __future__ import annotations

import fnmatch
from typing import Any, Optional

from loguru import logger

from ...mcp.config import MCPServerConfig
from ...mcp.installer import (
    InstallArgError,
    install_package,
    list_installed,
    validate_install_args,
)
from ...mcp.lifecycle import (
    AttachArgError,
    MCPLifecycleService,
    validate_attach_args,
)
from ...mcp.tool_wrapper import make_qualified_tool_name
from ..base import Tool, ToolPermission, ToolResult


class MCPManageTool(Tool):
    name = "mcp_manage"
    description = (
        "Attach, detach, or inspect MCP (Model Context Protocol) servers"
        " at runtime. MCP servers are the standard way to plug external"
        " capabilities — filesystem access, GitHub, browser automation,"
        " arxiv, custom HTTP APIs — into the agent without writing"
        " ZLAgent-specific code.\n\n"
        "Permission tier is **confirm**: every call triggers an IM yes/no"
        " before running. That's intentional — adding a server spawns a"
        " subprocess (stdio) or opens an outbound HTTP connection and"
        " exposes whatever tools the server advertises to the LLM, so"
        " the operator should review each attachment.\n\n"
        "Actions (set ``action`` exactly):\n"
        "  • ``list`` — no args; returns a compact summary of every"
        " currently-registered MCP server with its connection status and"
        " tool count.\n"
        "  • ``add`` — attach a new server. Required: ``name``,"
        " ``transport`` (``stdio`` or ``http``). For ``stdio``: ``command``"
        " plus optional ``args`` (list of strings) and ``env`` (string"
        " mapping). For ``http``: ``url`` (must start with ``http://`` or"
        " ``https://``) plus optional ``headers``. Optional everywhere:"
        " ``description``, ``enabled`` (default true), and"
        " ``tool_override_permission`` to promote individual tools to"
        " ``safe``.\n"
        "  • ``remove`` — detach a server. Required: ``name``. Tears"
        " down the connection, drops every ``mcp__<name>__*`` wrapper"
        " from the registry, and deletes the persistent row.\n"
        "  • ``reconnect`` — bounce one server. Required: ``name``."
        " Re-discovers its tool list, useful after the server itself"
        " was upgraded.\n"
        "  • ``inspect`` — full status + advertised tool catalog for one"
        " server. Required: ``name``. Read-only.\n"
        "  • ``install`` (v1.1.0) — fetch a package binary into the"
        " managed prefix so a follow-up ``add`` finds the command on"
        " PATH. Required: ``package_manager`` (npm/pip/uvx/git_npm/"
        "git_pip), ``package`` (registry name like 'amap-mcp-server' or"
        " '@modelcontextprotocol/server-time'; or git URL like"
        " 'https://github.com/org/repo' for git_npm/git_pip). Optional:"
        " ``extra_args`` (whitelisted), ``timeout_seconds`` (default 180,"
        " max 600), ``allow_scripts`` (default false — leave it false"
        " unless the package documentation explicitly requires"
        " postinstall scripts).\n"
        "  • ``install_and_add`` (v1.1.0+) — atomic combo: install the"
        " package then immediately attach the resulting binary as an"
        " MCP server. Saves one yes/no compared to running install +"
        " add separately. Pass the **union** of install args"
        " (package_manager / package / extra_args / timeout_seconds /"
        " allow_scripts) plus add args (name / transport / command /"
        " args / env / description / tool_override_permission). Both"
        " halves validate up-front so a typo in either rejects before"
        " any subprocess fires. If install fails, attach is skipped;"
        " if install succeeds but attach fails, the package stays on"
        " disk for retry.\n"
        "  • ``installed`` (v1.1.0) — list packages already installed"
        " in the managed prefix. Required: ``package_manager`` (npm/pip/"
        "uvx). Read-only.\n"
        "  • ``promote`` (v1.1.0) — flip an attached server's per-tool"
        " permission so the LLM can call it without yes/no every time."
        " Required: ``name`` and one of ``tool_name`` (single tool) or"
        " ``tool_pattern`` (glob like ``search_*`` or ``*`` for all);"
        " ``permission`` (``safe`` or ``confirm``, default ``safe``)."
        " Persists into the server config so the override survives"
        " restart. Use only on read-only / idempotent tools (geocode,"
        " search, list, get) — never on tools that mutate external"
        " state.\n\n"
        "**Workflow** for adding a new server end-to-end:\n"
        "  1. ``install_and_add`` in one shot (one yes/no), OR\n"
        "     1a. ``install`` the package → wait for ok=true.\n"
        "     1b. ``add`` with the command name (or ``npx -y <pkg>``"
        " for ad-hoc) → confirms the binary works and registers tools.\n"
        "  2. Optional: ``promote`` read-only tools to ``safe`` so they"
        " stop asking confirm.\n\n"
        "**Picking the right ``package_manager``** (v1.2.2 heuristics — read"
        " before installing):\n"
        "  • Defaults to **``pip``** for **Python-style names** ending in"
        " ``-mcp-server`` / ``-mcp`` / ``mcp-`` and **all-lowercase, no @"
        " scope** (e.g. ``arxiv-mcp-server``, ``mcp-server-fetch``,"
        " ``zlagent-mcp-time``). Many MCP servers labelled ``arxiv``,"
        " ``filesystem``, ``time``, ``slack`` are Python and only on PyPI.\n"
        "  • Use **``npm``** ONLY for **scoped packages** (``@org/name``,"
        " e.g. ``@modelcontextprotocol/server-time``,"
        " ``@valyu/arxiv-mcp``) or names that end in ``-node`` /"
        " ``-mcp-nodejs`` / contain ``js``. ``npx -y`` only works with npm"
        " packages.\n"
        "  • Use **``uvx``** when you've seen ``uvx run <pkg>`` in the"
        " package's README or it's listed under PyPI but the README says"
        " ``uv tool install``.\n"
        "  • Use **``git_npm`` / ``git_pip``** only when the package isn't"
        " on a registry yet (rare; reserve for fresh GitHub-only releases).\n"
        "  • If install fails with ``Python 3.x not found`` or"
        " ``ImportError`` on a hyphenated package, the package is Python"
        " — retry with ``package_manager='pip'``. If install fails with"
        " ``404 Not Found`` from npm, the package isn't on npm — retry"
        " with ``pip`` (or check spelling).\n"
        "  • ``arxiv-mcp-server`` (PyPI or npm) is still a **Python** entrypoint"
        " (often requires ``python3.11`` on PATH). Prefer ``uvx`` +"
        " ``package='arxiv-mcp-server'`` when README allows it; scoped npm"
        " packages such as ``@valyu/arxiv-mcp`` are separate codebases.\n"
        "  • **Never re-issue** the same ``install_and_add`` call with"
        " different package names hoping to find a real one — that's"
        " just guessing. If two tries fail, ask the user to confirm the"
        " package name + manager rather than burning more confirms.\n\n"
        "**Hard limits** for safety: name must match"
        " ``[A-Za-z_][A-Za-z0-9_-]{0,63}`` (becomes a tool prefix);"
        " command path is plain text (we don't shell-expand);"
        " environment variables are passed verbatim — never embed secrets"
        " from the LLM message text without operator review."
        " Install passes ``--ignore-scripts`` by default (postinstall"
        " safety); ``extra_args`` cannot override safety flags."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = True
    should_defer = True
    search_hint = (
        "mcp manage attach detach connect external server stdio http "
        "filesystem github playwright plugin extension install npm pip uvx "
        "promote permission safe confirm"
    )
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list", "add", "remove", "reconnect", "inspect",
                    "install", "install_and_add", "installed", "promote",
                ],
                "description": (
                    "Which operation to perform. ``list`` / ``installed``"
                    " are the safe entry points — call them first to see"
                    " what's attached / what's already installed."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Server name. Required for add/remove/reconnect/inspect."
                    " Becomes the ``mcp__<name>__<tool>`` prefix on every"
                    " tool exposed by this server, so keep it short and"
                    " descriptive (e.g. ``filesystem``, ``arxiv``,"
                    " ``github_main``). Must match"
                    " ``[A-Za-z_][A-Za-z0-9_-]{0,63}``."
                ),
            },
            "transport": {
                "type": "string",
                "enum": ["stdio", "http"],
                "description": (
                    "Connection method. ``stdio`` spawns a local subprocess"
                    " and speaks JSON-RPC over its stdin/stdout (the"
                    " standard MCP wire). ``http`` opens a streamable-HTTP"
                    " session against ``url`` (Anthropic's newer remote"
                    " transport)."
                ),
            },
            "command": {
                "type": "string",
                "description": (
                    "stdio only — the executable to launch (e.g. ``npx``,"
                    " ``python``, an absolute path)."
                ),
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "stdio only — arguments passed verbatim to ``command``."
                    " Use this for ``-y @modelcontextprotocol/server-X``"
                    " style invocations."
                ),
            },
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "stdio only — extra environment variables for the"
                    " subprocess. Server tokens / API keys go here."
                ),
            },
            "url": {
                "type": "string",
                "description": (
                    "http only — endpoint URL, must start with"
                    " ``http://`` or ``https://``."
                ),
            },
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "http only — extra request headers (auth tokens,"
                    " custom routing). Use ``Authorization: Bearer ...``"
                    " for token-protected endpoints."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Optional human-readable summary of what the server"
                    " does. Surfaced in ``list`` output and to the LLM"
                    " on every tool call."
                ),
            },
            "enabled": {
                "type": "boolean",
                "description": (
                    "Default true. Set false to register the row but skip"
                    " the live connection (useful when waiting on"
                    " credentials)."
                ),
            },
            "tool_override_permission": {
                "type": "object",
                "additionalProperties": {
                    "type": "string", "enum": ["safe", "confirm"],
                },
                "description": (
                    "Per-tool permission promotions. Tools default to"
                    " ``confirm`` (every call asks the operator). List"
                    " read-only tools here as ``safe`` to let the agent"
                    " call them without asking — e.g."
                    " ``{\"read_file\": \"safe\", \"list_directory\":"
                    " \"safe\"}`` for a filesystem server."
                ),
            },
            "package_manager": {
                "type": "string",
                "enum": ["npm", "pip", "uvx", "git_npm", "git_pip"],
                "description": (
                    "install / installed only — which package manager to"
                    " use. ``npm`` for JavaScript MCP servers"
                    " (most common). ``pip`` / ``uvx`` for Python"
                    " (uvx isolates each tool in its own venv;"
                    " prefer it for Python servers). ``git_npm`` /"
                    " ``git_pip`` for installing directly from a git"
                    " URL."
                ),
            },
            "package": {
                "type": "string",
                "description": (
                    "install only — registry package spec (e.g."
                    " ``amap-mcp-server``, ``@modelcontextprotocol/"
                    "server-time``, ``foo@1.2.3``) or git URL"
                    " (``https://github.com/org/repo`` /"
                    " ``git+https://...``). Validated against a tight"
                    " regex; no shell metachars."
                ),
            },
            "extra_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "install only — extra flags forwarded to the package"
                    " manager. Whitelisted: ``--prefix`` /"
                    " ``--script-shell`` / ``--global-option`` /"
                    " ``--unsafe-perm`` and friends are blocked. Most"
                    " calls leave this empty."
                ),
            },
            "timeout_seconds": {
                "type": "number",
                "description": (
                    "install only — kill the subprocess if it runs"
                    " longer. Default 180s, max 600s. Set higher for"
                    " slow networks or large dependency trees."
                ),
            },
            "allow_scripts": {
                "type": "boolean",
                "description": (
                    "install only — opt out of the default"
                    " ``--ignore-scripts`` safety flag. Leave false"
                    " unless the package documentation explicitly says"
                    " postinstall is required (rare). When true,"
                    " arbitrary code from the package can run during"
                    " install — operator review essential."
                ),
            },
            "tool_name": {
                "type": "string",
                "description": (
                    "promote only — the unqualified MCP tool name"
                    " (without the ``mcp__<server>__`` prefix) to flip."
                    " Either this or ``tool_pattern`` must be set."
                ),
            },
            "tool_pattern": {
                "type": "string",
                "description": (
                    "promote only — glob pattern (``*`` for all,"
                    " ``search_*`` for prefix match) matching multiple"
                    " unqualified tool names at once."
                ),
            },
            "permission": {
                "type": "string",
                "enum": ["safe", "confirm"],
                "description": (
                    "promote only — the new permission tier for the"
                    " matched tools. ``safe`` skips the IM yes/no on"
                    " every call (use for read-only / idempotent tools);"
                    " ``confirm`` puts a tool back behind the prompt."
                    " Defaults to ``safe``."
                ),
            },
        },
        "required": ["action"],
    }

    # v0.45 — read-only actions skip the confirm prompt entirely so
    # the user doesn't have to type ``yes`` to *look at* MCP state.
    # v1.1.0 added ``installed`` to this set (it just lists installed
    # packages; no host mutation).
    _READ_ONLY_ACTIONS = frozenset({"list", "inspect", "installed"})

    def __init__(
        self,
        *,
        lifecycle: MCPLifecycleService,
        manager,  # MCPManager — used for read-only inspect / list / promote
        registry,  # ToolRegistry — used for read-only inspect / promote
        store,    # MCPServerStore — used for read-only list/inspect / promote
        workspace_dir=None,  # Path | None — install logs go to logs/ here
    ) -> None:
        self._lifecycle = lifecycle
        self._manager = manager
        self._registry = registry
        self._store = store
        self._workspace_dir = workspace_dir

    def is_action_read_only(self, arguments: dict[str, Any] | None) -> bool:
        if not isinstance(arguments, dict):
            return False
        action = str(arguments.get("action") or "").strip().lower()
        return action in self._READ_ONLY_ACTIONS

    # -- public dispatch ----------------------------------------------------

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        action = (arguments or {}).get("action")
        if not isinstance(action, str):
            return ToolResult(
                ok=False, content="",
                error="missing required argument 'action'",
            )
        try:
            if action == "list":
                return self._action_list()
            if action == "inspect":
                return self._action_inspect(arguments)
            if action == "add":
                return await self._action_add(arguments)
            if action == "remove":
                return await self._action_remove(arguments)
            if action == "reconnect":
                return await self._action_reconnect(arguments)
            if action == "install":
                return await self._action_install(arguments)
            if action == "install_and_add":
                return await self._action_install_and_add(arguments)
            if action == "installed":
                return await self._action_installed(arguments)
            if action == "promote":
                return self._action_promote(arguments)
        except (AttachArgError, InstallArgError) as exc:
            return ToolResult(ok=False, content="", error=str(exc))
        except Exception as exc:  # noqa: BLE001 — surface to LLM, keep loop alive
            logger.exception("[mcp_manage] action {!r} crashed", action)
            return ToolResult(
                ok=False, content="",
                error=f"{type(exc).__name__}: {exc}",
            )
        return ToolResult(
            ok=False, content="",
            error=(
                f"unknown action {action!r}; valid actions: list, add,"
                " remove, reconnect, inspect, install, installed, promote"
            ),
        )

    # -- list ---------------------------------------------------------------

    def _action_list(self) -> ToolResult:
        statuses = self._manager.status() if self._manager is not None else []
        stored_by_name = {s.config.name: s for s in self._store.list_all()}
        rows: list[dict[str, Any]] = []
        for s in statuses:
            stored = stored_by_name.get(s.name)
            rows.append({
                "name": s.name,
                "transport": s.transport,
                "connected": s.connected,
                "enabled": s.enabled,
                "tool_count": s.tool_count,
                "breaker_open": s.breaker_open,
                "consecutive_failures": s.consecutive_failures,
                "description": s.description,
                "error": s.error,
                "source": stored.source if stored else "yaml",
            })
        connected = sum(1 for r in rows if r["connected"])
        body = {
            "count": len(rows),
            "connected": connected,
            "servers": rows,
        }
        return ToolResult(ok=True, content=_render_list(body))

    # -- inspect ------------------------------------------------------------

    def _action_inspect(self, args: dict[str, Any]) -> ToolResult:
        name = _require_str_local(args, "name")
        if self._manager is None:
            return ToolResult(
                ok=False, content="", error="MCP subsystem disabled",
            )
        cfg = self._manager.get_config(name)
        if cfg is None:
            return ToolResult(
                ok=False, content="",
                error=f"unknown MCP server '{name}'",
            )
        status = next(
            (s for s in self._manager.status() if s.name == name), None,
        )
        conn = self._manager.get_connection(name)
        tools_payload = []
        if conn is not None:
            for descriptor in conn.tools:
                qualified = make_qualified_tool_name(name, descriptor.name)
                registered = self._registry.get(qualified) is not None
                tools_payload.append({
                    "remote_name": descriptor.name,
                    "qualified_name": qualified,
                    "registered": registered,
                    "description": descriptor.description,
                })
        body = {
            "name": name,
            "transport": cfg.transport,
            "command": cfg.command,
            "args": list(cfg.args),
            "url": cfg.url,
            "description": cfg.description,
            "enabled": cfg.enabled,
            "connected": status.connected if status else False,
            "error": status.error if status else None,
            "tools": tools_payload,
        }
        return ToolResult(ok=True, content=_render_inspect(body))

    # -- add / remove / reconnect (delegate to lifecycle service) -----------

    async def _action_add(self, args: dict[str, Any]) -> ToolResult:
        if self._lifecycle is None:
            return ToolResult(
                ok=False, content="", error="MCP subsystem disabled",
            )
        cfg = validate_attach_args(args)
        outcome = await self._lifecycle.attach(
            cfg, source="im", created_by=_extract_created_by(args),
        )
        if not outcome.ok:
            # v0.45 — surface the tail of the MCP stderr log when an
            # attach fails. Previously the operator saw only "attach
            # failed for unknown reason" while the real cause (npx not
            # found / network blocked / package missing) was buried in
            # workspace/logs/mcp-stderr.log. This makes the failure
            # path self-describing without forcing a log dive.
            # v1.2.2 — scope the tail to *this* server so a failing
            # arxiv attach does not surface amap's API-key error.
            tail = _read_mcp_stderr_tail(server_name=cfg.name)
            error_msg = outcome.error or "attach failed for unknown reason"
            if tail:
                error_msg = (
                    f"{error_msg}\n\n--- mcp-stderr.log (last lines) ---\n"
                    f"{tail}"
                )
            return ToolResult(
                ok=False, content="", error=error_msg,
            )
        if outcome.persist_error:
            return ToolResult(
                ok=True, content=_render_add_partial(
                    cfg, outcome.registered, outcome.skipped,
                    persist_error=outcome.persist_error,
                ),
            )
        return ToolResult(
            ok=True,
            content=_render_add(cfg, outcome.registered, outcome.skipped),
        )

    async def _action_remove(self, args: dict[str, Any]) -> ToolResult:
        if self._lifecycle is None:
            return ToolResult(
                ok=False, content="", error="MCP subsystem disabled",
            )
        name = _require_str_local(args, "name")
        outcome = await self._lifecycle.detach(name)
        if not outcome.ok:
            return ToolResult(
                ok=False, content="",
                error=outcome.error or "detach failed",
            )
        return ToolResult(
            ok=True,
            content=_render_remove(
                name, outcome.unregistered,
                outcome.manager_dropped, outcome.store_deleted,
            ),
        )

    async def _action_reconnect(self, args: dict[str, Any]) -> ToolResult:
        if self._lifecycle is None:
            return ToolResult(
                ok=False, content="", error="MCP subsystem disabled",
            )
        name = _require_str_local(args, "name")
        outcome = await self._lifecycle.reconnect(name)
        if not outcome.ok:
            return ToolResult(
                ok=False, content="",
                error=outcome.error or "reconnect failed",
            )
        return ToolResult(
            ok=True,
            content=(
                f"Reconnected MCP server '{name}'. Dropped"
                f" {len(outcome.dropped)} old tool wrapper(s); registered"
                f" {outcome.re_registered} new one(s)."
            ),
        )

    # -- install (v1.1.0) ---------------------------------------------------

    async def _action_install(self, args: dict[str, Any]) -> ToolResult:
        """Install an MCP server's package binary.

        Decoupled from ``add`` so a failed install (network blip, bad
        package name, postinstall denial) doesn't leave the manager in
        a half-attached state. The follow-up ``add`` is what actually
        spawns the process and registers tools.
        """
        req = validate_install_args(args)
        outcome = await install_package(req, workspace_dir=self._workspace_dir)
        if outcome.ok:
            logger.info(
                "[mcp_manage] install OK ({} {}) duration={}ms",
                outcome.package_manager, outcome.package, outcome.duration_ms,
            )
            return ToolResult(ok=True, content=_render_install(outcome))
        logger.warning(
            "[mcp_manage] install failed ({} {}): {}",
            outcome.package_manager, outcome.package, outcome.error,
        )
        return ToolResult(
            ok=False, content=_render_install(outcome),
            error=outcome.error or "install failed",
        )

    # -- install_and_add (v1.1.0+) ------------------------------------------

    async def _action_install_and_add(self, args: dict[str, Any]) -> ToolResult:
        """Install a package then attach the resulting binary in one shot.

        Saves the operator one yes/no compared to running ``install`` and
        ``add`` separately. Failure modes are handled in order:

        * Install fails → return install failure verbatim, never call add
          (no orphaned manager state, no orphan registry entries).
        * Install succeeds but add fails → keep the installed package on
          disk (it's harmless — just not wired into the agent yet) and
          tell the operator they can retry ``add`` directly. The package
          can also be uninstalled later if the operator decides.
        * Both succeed → return a combined render covering install
          duration + attached server + tool count.

        Validation runs **both** halves up-front (install args + attach
        args) so a typo in either rejects before any subprocess fires —
        avoids the surprising case where install succeeds against a
        package the operator never intended.
        """
        # Pre-validate both halves before doing anything irreversible.
        # validate_install_args raises InstallArgError; validate_attach_args
        # raises AttachArgError. The dispatch wrapper catches both.
        install_req = validate_install_args(args)
        attach_cfg = validate_attach_args(args)

        # Install first.
        install_outcome = await install_package(
            install_req, workspace_dir=self._workspace_dir,
        )
        if not install_outcome.ok:
            logger.warning(
                "[mcp_manage] install_and_add: install failed ({} {}): {}",
                install_outcome.package_manager, install_outcome.package,
                install_outcome.error,
            )
            # Compose a result that's clearly install-failed (so the LLM
            # doesn't think the attach was the problem).
            return ToolResult(
                ok=False,
                content=_render_install(install_outcome),
                error=(
                    f"install_and_add aborted at install step: "
                    f"{install_outcome.error or 'install failed'}"
                ),
            )

        # Install succeeded — proceed to attach.
        if self._lifecycle is None:
            return ToolResult(
                ok=False,
                content=_render_install(install_outcome) + (
                    "\n\nNote: install succeeded but MCP subsystem is"
                    " disabled, so the server cannot be attached. The"
                    " package is on disk; enable MCP and retry add."
                ),
                error="MCP subsystem disabled; install completed",
            )

        attach_outcome = await self._lifecycle.attach(
            attach_cfg, source="im", created_by=_extract_created_by(args),
        )
        if not attach_outcome.ok:
            # v1.2.2 — scope stderr tail to attach_cfg.name so a prior
            # server's errors do not leak into this report.
            tail = _read_mcp_stderr_tail(server_name=attach_cfg.name)
            attach_error = (
                attach_outcome.error or "attach failed for unknown reason"
            )
            if tail:
                attach_error = (
                    f"{attach_error}\n\n--- mcp-stderr.log (last lines)"
                    f" ---\n{tail}"
                )
            logger.warning(
                "[mcp_manage] install_and_add: install OK but attach"
                " failed for {!r}: {}",
                attach_cfg.name, attach_outcome.error,
            )
            # Hybrid render: show install win + attach fail. The
            # package sits on disk for future retry.
            head = _render_install(install_outcome)
            tail_msg = (
                f"\n\nHowever, attaching server '{attach_cfg.name}'"
                f" FAILED:\n{attach_error}\n\nThe package is installed"
                " and on PATH. You can retry mcp_manage(action='add',"
                " ...) directly without re-installing."
            )
            return ToolResult(
                ok=False,
                content=head + tail_msg,
                error=f"attach failed after install: {attach_error}",
            )

        # Both succeeded — render the combined success.
        if attach_outcome.persist_error:
            attach_block = _render_add_partial(
                attach_cfg, attach_outcome.registered, attach_outcome.skipped,
                persist_error=attach_outcome.persist_error,
            )
        else:
            attach_block = _render_add(
                attach_cfg, attach_outcome.registered, attach_outcome.skipped,
            )
        return ToolResult(
            ok=True,
            content=(
                _render_install(install_outcome)
                + "\n\n"
                + attach_block
            ),
        )

    # -- installed (v1.1.0, read-only) --------------------------------------

    async def _action_installed(self, args: dict[str, Any]) -> ToolResult:
        """List packages installed in the managed prefix.

        Read-only by design; no confirm prompt (see
        :data:`_READ_ONLY_ACTIONS`). Useful for the agent to check
        before suggesting ``install`` — and for the operator to audit
        what's been added since deploy.
        """
        raw_manager = (args or {}).get("package_manager")
        if not isinstance(raw_manager, str) or not raw_manager.strip():
            return ToolResult(
                ok=False, content="",
                error=(
                    "missing required argument 'package_manager'"
                    " (one of: npm, pip, uvx)"
                ),
            )
        outcome = await list_installed(raw_manager.strip().lower())
        if not outcome.ok:
            return ToolResult(
                ok=False, content="",
                error=outcome.error or "list failed",
            )
        return ToolResult(ok=True, content=_render_installed(outcome))

    # -- promote (v1.1.0) ---------------------------------------------------

    def _action_promote(self, args: dict[str, Any]) -> ToolResult:
        """Flip a server's per-tool permission tier in place.

        Mutates both the live wrapper (so the next agent turn sees the
        new tier) and the persisted ``MCPServerConfig.tool_override_permission``
        (so a restart preserves the choice). No registry rebuild is
        needed because :class:`MCPTool` stores its permission as an
        instance attribute, not via dataclass slot — direct assignment
        works.
        """
        if self._manager is None:
            return ToolResult(
                ok=False, content="", error="MCP subsystem disabled",
            )
        name = _require_str_local(args, "name")
        cfg = self._manager.get_config(name)
        if cfg is None:
            return ToolResult(
                ok=False, content="",
                error=f"unknown MCP server '{name}'",
            )

        raw_perm = (args or {}).get("permission") or "safe"
        permission = str(raw_perm).strip().lower()
        if permission not in ("safe", "confirm"):
            return ToolResult(
                ok=False, content="",
                error=(
                    f"permission must be 'safe' or 'confirm', got"
                    f" {raw_perm!r}"
                ),
            )

        tool_name = (args or {}).get("tool_name")
        tool_pattern = (args or {}).get("tool_pattern")
        if not (tool_name or tool_pattern):
            return ToolResult(
                ok=False, content="",
                error=(
                    "promote requires either 'tool_name' (single tool)"
                    " or 'tool_pattern' (glob)"
                ),
            )
        if tool_name and tool_pattern:
            return ToolResult(
                ok=False, content="",
                error="set 'tool_name' OR 'tool_pattern', not both",
            )

        prefix = make_qualified_tool_name(name, "")
        target_perm = (
            ToolPermission.SAFE if permission == "safe"
            else ToolPermission.CONFIRM
        )
        is_safe = permission == "safe"

        # Walk every wrapper currently in the registry whose qualified
        # name belongs to this server. The ToolRegistry doesn't expose
        # an iterator over its dict, but ``definitions(...)`` lets us
        # enumerate; here we touch the underlying dict directly via
        # ``get`` after pre-filtering names.
        all_names = list(getattr(self._registry, "_tools", {}).keys())
        candidates: list = []
        for qname in all_names:
            if not qname.startswith(prefix):
                continue
            remote = qname[len(prefix):]
            if tool_name and remote != str(tool_name):
                continue
            if tool_pattern and not fnmatch.fnmatch(
                remote, str(tool_pattern),
            ):
                continue
            tool_obj = self._registry.get(qname)
            if tool_obj is not None:
                candidates.append((qname, remote, tool_obj))

        if not candidates:
            return ToolResult(
                ok=False, content="",
                error=(
                    f"no tools matched on server '{name}'"
                    f" (tool_name={tool_name!r}, tool_pattern={tool_pattern!r})"
                ),
            )

        changed: list[str] = []
        for qname, _remote, tool_obj in candidates:
            if tool_obj.permission == target_perm:
                continue
            tool_obj.permission = target_perm
            tool_obj.is_read_only = is_safe
            tool_obj.is_concurrency_safe = is_safe
            tool_obj.is_destructive = not is_safe
            changed.append(qname)

        # Persist into the in-memory MCPServerConfig and the SQLite
        # row so this survives a restart. ``tool_override_permission``
        # on the dataclass is a plain dict — we mutate in place because
        # the manager and store both hold references to the same cfg
        # instance (see lifecycle.attach).
        new_overrides = dict(cfg.tool_override_permission)
        for _qname, remote, _t in candidates:
            new_overrides[remote] = permission
        cfg.tool_override_permission = new_overrides

        store_persist_error: Optional[str] = None
        try:
            # ``upsert`` rewrites the row; we keep the same source
            # ('im') and created_by because the original attach
            # already recorded them and we don't have that context here.
            self._store.upsert(cfg, source="im", created_by=None)
        except Exception as exc:  # noqa: BLE001
            store_persist_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[mcp_manage] promote persist failed for {}: {}",
                name, exc,
            )

        logger.info(
            "[mcp_manage] promote {} -> {} ({} tools changed,"
            " {} matched)",
            name, permission, len(changed), len(candidates),
        )
        return ToolResult(
            ok=True,
            content=_render_promote(
                name, permission, candidates, changed,
                persist_error=store_persist_error,
            ),
        )


# -- helpers ------------------------------------------------------------------


def _require_str_local(args: dict[str, Any], key: str) -> str:
    """Local single-key validator (used by inspect/remove/reconnect actions
    where ``validate_attach_args`` would over-validate)."""
    raw = (args or {}).get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise AttachArgError(f"missing required argument '{key}'")
    return raw.strip()


def _extract_created_by(args: dict[str, Any]) -> Optional[str]:
    """Pull the IM identity out of args if the agent threaded it through.

    The agent loop does not currently inject this — reserved for a future
    audit improvement. Returning None today is fine; the DB column is
    nullable.
    """
    raw = (args or {}).get("_created_by")
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def _read_mcp_stderr_tail(
    *,
    max_lines: int = 25,
    max_chars: int = 2000,
    server_name: Optional[str] = None,
) -> str:
    """Return the tail of ``workspace/logs/mcp-stderr.log``.

    Used by ``_action_add`` / ``_action_install_and_add`` to embed real
    subprocess output in the failure message — installation usually
    fails because ``npx`` is missing or because the package URL is
    unreachable, and that detail lives in the stderr log not in the
    SDK's ready_error string.

    v1.2.2 — when ``server_name`` is provided we scan backward from
    EOF for the last ``===== [...] starting MCP server '<name>' =====``
    marker (written by ``transport.write_stderr_log_marker``) and
    return only the tail from that marker onwards. This avoids
    polluting ``server X`` failure reports with the **previous**
    server's stderr (e.g. a leaked ``amap`` API-key error surfacing
    when the operator is debugging ``arxiv``). Falls back to the
    plain full-file tail when no marker matches or ``server_name``
    is ``None``.
    """
    try:
        from ...mcp.transport import _stderr_log_path
    except Exception:  # noqa: BLE001
        return ""
    path = _stderr_log_path
    if path is None or not path.exists():
        return ""
    try:
        # Quick tail: read whole file (it's bounded by line buffering)
        # and slice the last N lines. For very large logs we slice by
        # bytes from the end first to keep the read cheap.
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            # When filtering by server we need enough context to find
            # the start marker; bump the read window for that path.
            read_window = 128 * 1024 if server_name else 32 * 1024
            if size > read_window:
                fh.seek(size - read_window)
                fh.readline()  # drop partial first line
            data = fh.read()
    except Exception:  # noqa: BLE001
        return ""

    if server_name:
        # Find the LAST ``starting MCP server '<name>'`` marker in the
        # window and slice from there. fall through to unfiltered
        # tail if no marker matched.
        marker = f"starting MCP server '{server_name}'"
        idx = data.rfind(marker)
        if idx >= 0:
            # Back up to the start of the marker line so the header is
            # part of the included tail (gives the operator a clear
            # "this is where I started looking" anchor).
            line_start = data.rfind("\n", 0, idx)
            data = data[(line_start + 1) if line_start >= 0 else 0:]

    tail_lines = data.rstrip("\n").splitlines()[-max_lines:]
    tail = "\n".join(tail_lines)
    if len(tail) > max_chars:
        tail = "…\n" + tail[-max_chars:]
    return tail.strip()


# -- IM-friendly renderers ----------------------------------------------------

def _render_list(body: dict[str, Any]) -> str:
    if body["count"] == 0:
        return "No MCP servers attached. Use action=add to attach one."
    lines = [
        f"MCP servers: {body['connected']}/{body['count']} connected.",
        "",
    ]
    for s in body["servers"]:
        marker = "✓" if s["connected"] else ("·" if s["enabled"] else "○")
        head = (
            f"{marker} {s['name']} [{s['transport']}, source={s['source']}]"
            f" — {s['tool_count']} tool(s)"
        )
        lines.append(head)
        if s["description"]:
            lines.append(f"    {s['description']}")
        if not s["connected"] and s["error"]:
            lines.append(f"    error: {s['error']}")
        if s["breaker_open"]:
            lines.append(
                f"    breaker open ({s['consecutive_failures']} consecutive"
                " failures)"
            )
    return "\n".join(lines)


def _render_inspect(body: dict[str, Any]) -> str:
    lines = [
        f"Server: {body['name']} [{body['transport']}]",
        f"Connected: {body['connected']}    Enabled: {body['enabled']}",
    ]
    if body["transport"] == "stdio":
        if body["command"]:
            cmd = body["command"]
            if body["args"]:
                cmd = f"{cmd} {' '.join(body['args'])}"
            lines.append(f"Command: {cmd}")
    else:
        lines.append(f"URL: {body['url']}")
    if body["description"]:
        lines.append(f"Description: {body['description']}")
    if body["error"]:
        lines.append(f"Error: {body['error']}")
    if body["tools"]:
        lines.append(f"Tools ({len(body['tools'])}):")
        for t in body["tools"]:
            registered = "" if t["registered"] else " (NOT registered)"
            head = f"  • {t['qualified_name']}{registered}"
            lines.append(head)
            if t["description"]:
                desc = t["description"].splitlines()[0][:120]
                lines.append(f"      {desc}")
    else:
        lines.append("Tools: (none discovered)")
    return "\n".join(lines)


def _render_add(
    cfg: MCPServerConfig, registered: int, skipped: list[tuple[str, str]],
) -> str:
    lines = [
        f"Attached MCP server '{cfg.name}' ({cfg.transport}).",
        f"Registered {registered} tool wrapper(s) into the agent registry.",
    ]
    if not cfg.enabled:
        lines.append("Note: enabled=false, no live connection started.")
    if skipped:
        lines.append(f"Skipped {len(skipped)} tool(s):")
        for tool_name, reason in skipped[:6]:
            lines.append(f"  • {tool_name}: {reason}")
    if registered > 0:
        lines.append(
            "On the next turn the agent can call them as"
            f" mcp__{cfg.name}__<tool_name>."
        )
    return "\n".join(lines)


def _render_add_partial(
    cfg: MCPServerConfig,
    registered: int,
    skipped: list[tuple[str, str]],
    *,
    persist_error: str,
) -> str:
    base = _render_add(cfg, registered, skipped)
    return base + (
        "\n\nWARNING: persisted save failed — the server is live but"
        f" will be lost on restart. Reason: {persist_error}"
    )


def _render_install(outcome) -> str:
    """Render an InstallResult into IM-friendly Markdown.

    Success path keeps the message short — the operator just wants to
    know "did it work, what did I get". Failure path inlines the
    stderr tail since the LLM otherwise has nothing concrete to act on.
    """
    head = (
        f"Installed {outcome.package_manager} package"
        f" '{outcome.package}' in {outcome.duration_ms}ms."
    ) if outcome.ok else (
        f"Install of {outcome.package_manager} package"
        f" '{outcome.package}' FAILED ({outcome.duration_ms}ms,"
        f" exit={outcome.exit_code})."
    )
    lines = [head]
    if outcome.command:
        lines.append(f"Command: {' '.join(outcome.command)}")
    if outcome.error:
        lines.append(f"Error: {outcome.error}")
    if outcome.stdout_tail:
        lines.append("--- stdout (tail) ---")
        lines.append(outcome.stdout_tail)
    if outcome.stderr_tail:
        lines.append("--- stderr (tail) ---")
        lines.append(outcome.stderr_tail)
    if outcome.ok:
        lines.append(
            "Next: call mcp_manage(action='add', ...) to attach the"
            " server. The binary should now be on PATH."
        )
    return "\n".join(lines)


def _render_installed(outcome) -> str:
    """Render a ListResult into a compact table-ish dump."""
    if not outcome.packages:
        return (
            f"No {outcome.package_manager} packages installed in the"
            " managed prefix."
        )
    lines = [
        f"{outcome.package_manager} packages ({len(outcome.packages)}):",
    ]
    for pkg in outcome.packages:
        version = f" {pkg.version}" if pkg.version else ""
        lines.append(f"  • {pkg.name}{version}")
    return "\n".join(lines)


def _render_promote(
    name: str,
    permission: str,
    candidates: list,
    changed: list[str],
    *,
    persist_error: Optional[str] = None,
) -> str:
    """Render the outcome of a promote call.

    ``candidates`` is the list of tools that matched the filter;
    ``changed`` is the subset whose permission actually moved (the rest
    were already at the requested tier — counted as already-up-to-date).
    """
    matched = len(candidates)
    moved = len(changed)
    already = matched - moved
    lines = [
        f"Promoted {moved} tool(s) on MCP server '{name}' to"
        f" '{permission}' (matched {matched}, already at target {already})."
    ]
    if changed:
        for qname in changed[:8]:
            lines.append(f"  • {qname}  →  {permission}")
        if moved > 8:
            lines.append(f"  • ... and {moved - 8} more")
    if persist_error:
        lines.append(
            f"WARNING: persistent save failed — change is live but"
            f" will be lost on restart. Reason: {persist_error}"
        )
    if permission == "safe":
        lines.append(
            "These tools will no longer ask for yes/no confirmation"
            " when called. Reverse with"
            f" mcp_manage(action='promote', name='{name}', "
            "tool_pattern='*', permission='confirm')."
        )
    return "\n".join(lines)


def _render_remove(
    name: str, unregistered: list[str], manager_ok: bool, store_ok: bool,
) -> str:
    lines = [
        f"Detached MCP server '{name}'.",
        f"Unregistered {len(unregistered)} tool wrapper(s).",
    ]
    if not manager_ok:
        lines.append(
            "Note: manager had no record of the server (already detached?)."
        )
    if not store_ok:
        lines.append(
            "Note: persistent row was not removed (yaml-sourced or"
            " already gone). YAML-seeded servers must be removed by"
            " editing config/mcp_servers.yaml and restarting."
        )
    return "\n".join(lines)


__all__ = ["MCPManageTool"]
