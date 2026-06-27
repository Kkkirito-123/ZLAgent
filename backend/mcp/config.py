"""MCP server configuration loader.

Reads ``config/mcp_servers.yaml`` (path comes from ``Settings.mcp_config_path``)
into a ``dict[name, MCPServerConfig]`` ready to feed into :class:`MCPManager`.

Schema (a single example covering every field — most are optional)::

    servers:
      filesystem:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/app/workspace"]
        env: {}                       # extra env to pass to the subprocess
        enabled: true                 # default true
        connect_timeout_seconds: 30   # default 30
        call_timeout_seconds: 120     # default 120; per tool call wall clock
        tools:
          include: ["read_file", "list_directory"]   # whitelist; null = all
          exclude: ["write_file"]                    # blacklist; default []
          override_permission:                       # default empty
            read_file: safe                          # promote read-only
        description: "Filesystem access scoped to /app/workspace."

      github:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_TOKEN}"   # env interpolation

The loader applies ``${VAR}`` substitution to every string value (including
nested values inside ``env`` and ``args``) using the process environment.
Missing variables resolve to empty string — same behaviour as Hermes.

Validation is conservative: any malformed entry raises ``ValueError`` with a
``server '<name>': <reason>`` prefix so the operator can find and fix it
without diving into the parser.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger


# -- Permission override token ------------------------------------------------

_VALID_PERMISSION_OVERRIDES = frozenset({"safe", "confirm"})

# Pattern for ${VAR} or ${VAR:-default} interpolation. We deliberately keep
# this simple — no shell-style nested expansion, no command substitution.
_INTERP_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# -- Public dataclass ---------------------------------------------------------

@dataclass(slots=True)
class MCPServerConfig:
    """Validated configuration for a single MCP server.

    Frozen-ish: callers should treat every field as read-only after
    construction. We don't enable ``frozen=True`` because :func:`load_mcp_config`
    needs to set ``name`` after the dict is parsed (the name is the YAML key,
    not a body field), and frozen would force a re-construction.
    """

    name: str
    # stdio servers set ``command``; HTTP servers set ``url``. Exactly
    # one of the two must be present (loader enforces). ``command`` stays
    # required-looking in the dataclass for stdio backward compat; HTTP
    # entries set it to "" and put the endpoint in ``url``.
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # v0.24 HTTP transport
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    connect_timeout_seconds: float = 30.0
    call_timeout_seconds: float = 120.0
    # tool_include = None means "all tools"; empty tuple means "no tools"
    # (which would disable the server effectively — we let it through anyway).
    tool_include: Optional[tuple[str, ...]] = None
    tool_exclude: frozenset[str] = field(default_factory=frozenset)
    # Tool name → "safe" or "confirm". Default for any tool not listed here
    # is "confirm" (see tool_wrapper.py). MCP tools that mutate external
    # state should NEVER be promoted to safe.
    tool_override_permission: dict[str, str] = field(default_factory=dict)
    description: str = ""

    @property
    def transport(self) -> str:
        """Return ``"http"`` if this is an HTTP server, else ``"stdio"``."""
        return "http" if self.url else "stdio"

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Apply include/exclude filters, return True if the tool should register."""
        if tool_name in self.tool_exclude:
            return False
        if self.tool_include is not None and tool_name not in self.tool_include:
            return False
        return True

    def permission_for(self, tool_name: str) -> str:
        """Return ``"safe"`` or ``"confirm"`` for ``tool_name``.

        Default is ``"confirm"`` — MCP tools are external code we don't fully
        trust. The operator must explicitly opt a tool into ``"safe"`` via
        ``tool_override_permission`` if they want it auto-executed.
        """
        return self.tool_override_permission.get(tool_name, "confirm")


# -- Interpolation helper -----------------------------------------------------

def _interpolate(value: Any) -> Any:
    """Recursively expand ``${VAR}`` references using ``os.environ``."""
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), "")
        return _INTERP_RE.sub(_replace, value)
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


# -- Top-level loader ---------------------------------------------------------

def load_mcp_config(path: Path | str) -> dict[str, MCPServerConfig]:
    """Read and validate the MCP server config file.

    Returns ``{}`` if the file does not exist (so an absent config simply
    disables MCP rather than crashing the boot). Raises ``ValueError`` with a
    descriptive message for any malformed entry.
    """
    p = Path(path)
    if not p.is_file():
        logger.info("[mcp] config file not found at {} — MCP disabled", p)
        return {}

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"failed to parse {p}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top-level must be a mapping, got {type(raw).__name__}")

    raw_servers = raw.get("servers") or {}
    if not isinstance(raw_servers, dict):
        raise ValueError(f"{p}: 'servers' must be a mapping, got {type(raw_servers).__name__}")

    out: dict[str, MCPServerConfig] = {}
    for name, body in raw_servers.items():
        cfg = _parse_server(str(name), body)
        out[cfg.name] = cfg
    return out


def _parse_server(name: str, body: Any) -> MCPServerConfig:
    """Parse one server entry. Raises ``ValueError`` with a clear prefix."""

    def fail(reason: str) -> "ValueError":
        return ValueError(f"server '{name}': {reason}")

    if not isinstance(body, dict):
        raise fail(f"body must be a mapping, got {type(body).__name__}")
    if not name or not isinstance(name, str) or "." in name or "/" in name:
        # Names go into tool prefixes ("mcp__<name>__<tool>"); enforce the
        # same identifier rules OpenAI/DeepSeek allow for tool names.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            raise fail("name must match [A-Za-z_][A-Za-z0-9_-]* (letters, digits, _ and -)")

    body = _interpolate(body)

    # exactly one of ``command`` (stdio) or ``url`` (HTTP) must be set.
    raw_command = body.get("command")
    raw_url = body.get("url")
    has_command = bool(raw_command and isinstance(raw_command, str))
    has_url = bool(raw_url and isinstance(raw_url, str))
    if has_command and has_url:
        raise fail(
            "'command' (stdio) and 'url' (http) are mutually exclusive — pick one"
        )
    if not has_command and not has_url:
        raise fail(
            "either 'command' (stdio transport) or 'url' (http transport) is required"
        )

    if has_url:
        url = str(raw_url).strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise fail(
                "'url' must start with http:// or https://"
            )
        command = ""
    else:
        command = str(raw_command)
        url = ""

    raw_args = body.get("args") or []
    if not isinstance(raw_args, list):
        raise fail("'args' must be a list of strings")
    args = tuple(str(a) for a in raw_args)
    if has_url and args:
        raise fail("'args' is only valid for stdio transport")

    raw_env = body.get("env") or {}
    if not isinstance(raw_env, dict):
        raise fail("'env' must be a mapping of name -> value")
    env = {str(k): str(v) for k, v in raw_env.items()}
    if has_url and env:
        raise fail("'env' is only valid for stdio transport (use 'headers' for http)")

    raw_headers = body.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise fail("'headers' must be a mapping of name -> value")
    headers = {str(k): str(v) for k, v in raw_headers.items()}
    if not has_url and headers:
        raise fail("'headers' is only valid for http transport (use 'env' for stdio)")

    enabled = bool(body.get("enabled", True))

    try:
        connect_timeout = float(body.get("connect_timeout_seconds", 30.0))
    except (TypeError, ValueError):
        raise fail("'connect_timeout_seconds' must be a number")
    if connect_timeout <= 0 or connect_timeout > 600:
        raise fail("'connect_timeout_seconds' must be in (0, 600]")

    try:
        call_timeout = float(body.get("call_timeout_seconds", 120.0))
    except (TypeError, ValueError):
        raise fail("'call_timeout_seconds' must be a number")
    if call_timeout <= 0 or call_timeout > 1800:
        raise fail("'call_timeout_seconds' must be in (0, 1800]")

    # tools sub-block ---------------------------------------------------------
    tools_body = body.get("tools") or {}
    if not isinstance(tools_body, dict):
        raise fail("'tools' must be a mapping")

    tool_include: Optional[tuple[str, ...]]
    raw_include = tools_body.get("include")
    if raw_include is None:
        tool_include = None
    elif isinstance(raw_include, list):
        tool_include = tuple(str(x) for x in raw_include)
    else:
        raise fail("'tools.include' must be a list or null")

    raw_exclude = tools_body.get("exclude") or []
    if not isinstance(raw_exclude, list):
        raise fail("'tools.exclude' must be a list")
    tool_exclude = frozenset(str(x) for x in raw_exclude)

    raw_perm = tools_body.get("override_permission") or {}
    if not isinstance(raw_perm, dict):
        raise fail("'tools.override_permission' must be a mapping")
    overrides: dict[str, str] = {}
    for tool_name, perm in raw_perm.items():
        perm_str = str(perm).strip().lower()
        if perm_str not in _VALID_PERMISSION_OVERRIDES:
            raise fail(
                f"'tools.override_permission.{tool_name}' must be one of"
                f" {sorted(_VALID_PERMISSION_OVERRIDES)}, got {perm!r}"
            )
        overrides[str(tool_name)] = perm_str

    description = str(body.get("description") or "").strip()

    return MCPServerConfig(
        name=name,
        command=command,
        args=args,
        env=env,
        url=url,
        headers=headers,
        enabled=enabled,
        connect_timeout_seconds=connect_timeout,
        call_timeout_seconds=call_timeout,
        tool_include=tool_include,
        tool_exclude=tool_exclude,
        tool_override_permission=overrides,
        description=description,
    )
