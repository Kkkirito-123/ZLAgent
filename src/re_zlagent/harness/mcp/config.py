"""Host-owned configuration for bounded local MCP servers."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROOT_KEYS = {"servers"}
_SERVER_KEYS = {
    "id",
    "transport",
    "command",
    "args",
    "cwd",
    "env",
    "tools",
    "approved",
    "startup_timeout_seconds",
    "request_timeout_seconds",
}


class McpConfigurationError(ValueError):
    """Raised when host MCP configuration is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """One explicitly approved local stdio MCP server."""

    id: str
    command: str
    args: tuple[str, ...] = field(default_factory=tuple)
    cwd: Path | None = None
    env_refs: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    startup_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "env_refs", tuple(self.env_refs))
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))

    @property
    def display_command(self) -> str:
        """Return the exact approved command without environment values."""

        return shlex.join((self.command, *self.args))

    def resolve_environment(self, environ: Mapping[str, str]) -> dict[str, str]:
        """Resolve named host variables without retaining their values in config."""

        resolved: dict[str, str] = {}
        missing: list[str] = []
        for child_name, host_name in self.env_refs:
            value = environ.get(host_name)
            if value is None:
                missing.append(host_name)
                continue
            resolved[child_name] = value
        if missing:
            raise McpConfigurationError(
                f"MCP server {self.id!r} is missing environment variables: "
                + ", ".join(sorted(missing))
            )
        return resolved


@dataclass(frozen=True, slots=True)
class McpConfig:
    """Validated MCP server set loaded from one host-owned JSON file."""

    servers: tuple[McpServerConfig, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "servers", tuple(self.servers))


def load_mcp_config(path: Path) -> McpConfig:
    """Load and fail closed on malformed or unapproved MCP configuration."""

    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise McpConfigurationError(
            f"MCP config file does not exist: {config_path}"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise McpConfigurationError(
            f"MCP config file cannot be read as JSON: {config_path}"
        ) from exc
    data = _object(raw, "MCP config")
    _reject_unknown(data, _ROOT_KEYS, "MCP config")
    servers_raw = data.get("servers")
    if not isinstance(servers_raw, list) or not servers_raw:
        raise McpConfigurationError("MCP config servers must be a non-empty list")
    servers = tuple(
        _parse_server(item, index=index, config_dir=config_path.parent.resolve())
        for index, item in enumerate(servers_raw)
    )
    ids = [server.id for server in servers]
    if len(ids) != len(set(ids)):
        raise McpConfigurationError("MCP server ids must be unique")
    return McpConfig(servers=servers)


def _parse_server(
    raw: Any,
    *,
    index: int,
    config_dir: Path,
) -> McpServerConfig:
    label = f"MCP config servers[{index}]"
    data = _object(raw, label)
    _reject_unknown(data, _SERVER_KEYS, label)
    server_id = _required_string(data, "id", label)
    if not _SERVER_ID_RE.fullmatch(server_id):
        raise McpConfigurationError(
            f"{label}.id must match {_SERVER_ID_RE.pattern}"
        )
    transport = data.get("transport", "stdio")
    if transport != "stdio":
        raise McpConfigurationError(f"{label}.transport must be 'stdio'")
    command = _required_string(data, "command", label)
    args = _string_list(data.get("args", []), f"{label}.args")
    if data.get("approved") is not True:
        exact = shlex.join((command, *args))
        raise McpConfigurationError(
            f"MCP server {server_id!r} command requires explicit approval: {exact}"
        )
    allowed_tools = _string_list(data.get("tools"), f"{label}.tools")
    if not allowed_tools:
        raise McpConfigurationError(f"{label}.tools must be a non-empty allowlist")
    if len(allowed_tools) > 64:
        raise McpConfigurationError(f"{label}.tools cannot contain more than 64 names")
    if any(len(name) > 128 for name in allowed_tools):
        raise McpConfigurationError(
            f"{label}.tools names cannot exceed 128 characters"
        )
    if len(allowed_tools) != len(set(allowed_tools)):
        raise McpConfigurationError(f"{label}.tools contains duplicate names")
    env_refs = _env_refs(data.get("env", {}), f"{label}.env")
    cwd = _cwd(data.get("cwd"), config_dir=config_dir, label=f"{label}.cwd")
    return McpServerConfig(
        id=server_id,
        command=command,
        args=args,
        cwd=cwd,
        env_refs=env_refs,
        allowed_tools=allowed_tools,
        startup_timeout_seconds=_timeout(
            data.get("startup_timeout_seconds", 10.0),
            f"{label}.startup_timeout_seconds",
        ),
        request_timeout_seconds=_timeout(
            data.get("request_timeout_seconds", 30.0),
            f"{label}.request_timeout_seconds",
        ),
    )


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise McpConfigurationError(f"{label} must be a JSON object")
    return dict(value)


def _reject_unknown(
    data: dict[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    unknown = sorted(set(data).difference(allowed))
    if unknown:
        raise McpConfigurationError(
            f"{label} contains unknown fields: {', '.join(unknown)}"
        )


def _required_string(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise McpConfigurationError(f"{label}.{key} must be a non-empty string")
    return value


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or "\x00" in item
        for item in value
    ):
        raise McpConfigurationError(f"{label} must be a string list")
    return tuple(value)


def _env_refs(value: Any, label: str) -> tuple[tuple[str, str], ...]:
    data = _object(value, label)
    refs: list[tuple[str, str]] = []
    for child_name, host_name in sorted(data.items()):
        if not _ENV_NAME_RE.fullmatch(child_name):
            raise McpConfigurationError(f"{label} contains invalid child env name")
        if not isinstance(host_name, str) or not _ENV_NAME_RE.fullmatch(host_name):
            raise McpConfigurationError(
                f"{label}.{child_name} must name a host environment variable"
            )
        refs.append((child_name, host_name))
    return tuple(refs)


def _cwd(value: Any, *, config_dir: Path, label: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise McpConfigurationError(f"{label} must be a non-empty path string")
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (config_dir / path).resolve()
    if not resolved.is_dir():
        raise McpConfigurationError(f"{label} is not an existing directory: {resolved}")
    return resolved


def _timeout(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise McpConfigurationError(f"{label} must be a number")
    resolved = float(value)
    if not 0 < resolved <= 300:
        raise McpConfigurationError(f"{label} must be within (0, 300]")
    return resolved
