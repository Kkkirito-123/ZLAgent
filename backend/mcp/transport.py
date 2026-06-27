"""Transport-layer helpers for MCP stdio servers.

Two responsibilities:

1. Build a ``StdioServerParameters`` with a *filtered* environment so we
   don't leak the full host env to a third-party subprocess. Mirrors
   Hermes' :func:`_build_safe_env`.

2. Provide a single shared file handle for stdio subprocess stderr so
   FastAPI's own stdout/log isn't corrupted by spurious banners
   (``slack-mcp-server``, FastMCP, etc. write a lot of startup noise).
   Mirrors Hermes' ``_get_mcp_stderr_log``.

Streamable-HTTP transport is intentionally not implemented in v0.14.
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TextIO

from loguru import logger
from mcp import StdioServerParameters

from .config import MCPServerConfig


# Baseline env vars that are safe to pass through unchanged. Anything not in
# this set must be explicitly listed by the operator in ``server.env``.
# Same idea as Hermes' filter — the threat model is "subprocess shouldn't
# automatically see OPENAI_API_KEY just because the bot has one in env".
_SAFE_BASELINE_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        # User-site installs (``pip install --user`` / ZLAgent Dockerfile
        # ``PYTHONUSERBASE=/app/.packages/pip``) — without this, stdio MCP
        # children that spawn ``python -m ...`` cannot see packages the
        # installer dropped under the managed prefix.
        "PYTHONUSERBASE",
        # Node / npm tooling commonly needs these:
        "APPDATA",
        "LOCALAPPDATA",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramData",
        "NODE_PATH",
    }
)

# XDG_* / NPM_CONFIG_* / UV_* / PIP_* prefix-matched on top of the explicit
# baseline; many CLI tools depend on these and they're rarely sensitive on
# their own. ``UV_*`` and ``PIP_*`` are required so ``uvx``/``uv``/``pip``-
# backed MCP servers inherit ``UV_CACHE_DIR`` / ``UV_TOOL_DIR`` /
# ``UV_TOOL_BIN_DIR`` / ``PIP_USER`` from the container, otherwise they
# fall back to HOME-relative defaults (``~/.cache/uv``,
# ``~/.local/share/uv/tools``) which ``zlagent`` (uid 10001) cannot write.
_SAFE_PREFIX_MATCH = (
    "XDG_",
    "NPM_CONFIG_",
    "UV_",
    "PIP_",
    "SSL_CERT_",
    "SSH_AUTH_SOCK",
)


# --- Stderr log handle (singleton) ------------------------------------------

_stderr_log_lock = threading.Lock()
_stderr_log_fh: Optional[TextIO] = None
_stderr_log_path: Optional[Path] = None


def configure_stderr_log_dir(workspace_dir: Path) -> Path:
    """Choose where stdio subprocess stderr should be tee'd.

    Called once during app startup. We pick ``<workspace>/logs/mcp-stderr.log``
    so the operator finds it next to the other logs. The directory is created
    here if missing.
    """
    global _stderr_log_path
    log_dir = Path(workspace_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _stderr_log_path = log_dir / "mcp-stderr.log"
    return _stderr_log_path


def get_stderr_log() -> TextIO:
    """Return a shared append-mode handle for MCP subprocess stderr.

    Falls back to the process's own stderr if the log file cannot be opened
    (preserves Hermes behaviour: never lose error output silently).
    """
    global _stderr_log_fh
    with _stderr_log_lock:
        if _stderr_log_fh is not None:
            return _stderr_log_fh
        target = _stderr_log_path
        if target is None:
            _stderr_log_fh = sys.stderr
            return _stderr_log_fh
        try:
            # Line-buffered, append, replace bad bytes — same recipe as
            # Hermes. We need a real file descriptor because asyncio's
            # subprocess machinery writes the child's stderr via fd, not
            # through Python.
            fh = open(target, "a", encoding="utf-8", errors="replace", buffering=1)
            fh.fileno()  # sanity-check there's a real fd
            _stderr_log_fh = fh
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp] could not open stderr log {}: {}", target, exc)
            try:
                _stderr_log_fh = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                _stderr_log_fh = sys.stderr
        return _stderr_log_fh


def write_stderr_log_marker(server_name: str) -> None:
    """Write a session header to the stderr log so operators can search by server."""
    try:
        fh = get_stderr_log()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"\n===== [{ts}] starting MCP server '{server_name}' =====\n")
        fh.flush()
    except Exception:  # noqa: BLE001
        pass


# --- Env filter --------------------------------------------------------------

def build_safe_env(extra_env: dict[str, str]) -> dict[str, str]:
    """Compose a minimal env dict for an MCP stdio subprocess.

    Always honours ``extra_env`` (those are explicit operator-blessed values,
    typically with ``${VAR}`` interpolation already resolved by config.py).
    Everything else has to come from the safe baseline / safe prefix sets.
    """
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _SAFE_BASELINE_ENV or any(key.startswith(p) for p in _SAFE_PREFIX_MATCH):
            out[key] = value
    # Operator overrides win — they're explicit.
    for key, value in (extra_env or {}).items():
        out[key] = value
    return out


def build_stdio_params(cfg: MCPServerConfig) -> StdioServerParameters:
    """Translate our :class:`MCPServerConfig` into the SDK's parameters object."""
    return StdioServerParameters(
        command=cfg.command,
        args=list(cfg.args),
        env=build_safe_env(cfg.env),
        # ``encoding="utf-8"`` is the SDK default but we set it explicitly to
        # avoid surprises on Windows hosts whose default codepage is GBK
        # (which is the case for the user's local dev environment).
        encoding="utf-8",
        encoding_error_handler="replace",
    )


# --- Sanitising error text returned to LLM ----------------------------------

# Patterns that look like credentials. Replace with [REDACTED] before the
# error string is shown to the LLM (which would otherwise echo it back to
# the user, or worse, save it to memory). Same intent as Hermes' helper.
import re  # noqa: E402 — co-located with the patterns it builds

_CRED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{20,}", re.IGNORECASE),
    re.compile(r"(?i)\b(sk|pk)-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bghp_[A-Za-z0-9]{20,}\b"),  # GitHub PAT
    re.compile(r"(?i)\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),  # Slack token
    re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
]


def sanitize_error(text: str) -> str:
    """Strip likely credentials from a server error message."""
    if not isinstance(text, str) or not text:
        return ""
    redacted = text
    for pattern in _CRED_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _reset_for_tests() -> None:
    """Reset module-level singletons. Used only by smoke tests."""
    global _stderr_log_fh, _stderr_log_path
    with _stderr_log_lock:
        if _stderr_log_fh is not None and _stderr_log_fh not in (sys.stderr, sys.stdout):
            try:
                _stderr_log_fh.close()
            except Exception:
                pass
        _stderr_log_fh = None
        _stderr_log_path = None
