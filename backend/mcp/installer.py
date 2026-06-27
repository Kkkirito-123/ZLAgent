"""Runtime installer for MCP server packages (v1.1.0).

ZLAgent's MCP subsystem (``MCPManager`` / ``MCPLifecycleService``) attaches
to MCP servers by spawning a stdio subprocess from a configured ``command``.
That assumes the binary is already on ``PATH`` — but for many servers the
binary lives in an npm/pypi package the operator hasn't downloaded yet.

This module fills that gap. It exposes :func:`install_package`, an async
helper that wraps ``npm install -g`` / ``pip install --user`` /
``uv tool install`` (and their git counterparts) with strict argument
validation, postinstall-script suppression, and a tail-of-output shape
the IM tool can render. The MCP tool layer
(``backend.tools.builtins.mcp_manage``) drives the workflow:

* Operator says "install amap" → ``mcp_manage(action='install', ...)``
* Tool calls :func:`install_package` to drop the binary into the managed
  ``/app/.packages/npm`` (or ``/app/.packages/pip``) prefix.
* Operator says "now attach" → ``mcp_manage(action='add', command='amap-mcp-server', ...)``
  finds the binary on PATH (the Dockerfile pre-pends both prefixes).

Security model lifted from openclaw's ``safe-package-install.ts``:

* ``--ignore-scripts`` is **always** passed to npm install — postinstall
  scripts are the #1 supply-chain attack vector.
* Package names match a tight regex; no shell metachars survive validation.
* Extra args run through a denylist (``--prefix``, ``--script-shell``,
  ``--global-option``, ``--unsafe-perm`` and friends) so an LLM cannot
  pivot a benign install into "rm -rf via custom build script".
* Subprocess runs with a bounded timeout (default 180s, max 600s).
* Stdout/stderr tails are captured to ``workspace/logs/mcp-install.log``
  for postmortem.

This file deliberately does **not** touch the MCP manager / registry. The
operator must make a separate ``mcp_manage(action='add')`` call after the
install completes — the install action persists nothing, so a half-finished
install is recoverable by re-running it.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger


# -- Validation regexes -------------------------------------------------------

# npm package: bare ``foo``, scoped ``@org/foo``, optionally with a
# version selector tail ``@1.2.3`` / ``@^1`` / ``@latest``. We allow the
# selector here so users can pin versions; the spec is bounded to keep
# the surface tight.
_PKG_NAME_RE = re.compile(
    r"^@?[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}"           # head (or scope)
    r"(?:/[a-zA-Z0-9][a-zA-Z0-9._-]{0,63})?"        # optional /pkg for scoped
    r"(?:@[a-zA-Z0-9][a-zA-Z0-9._^~<>=*+-]{0,63})?$"  # optional @version
)

# git URL: https or git+https only, host whitelist not enforced (any
# self-hosted gitea instance should work) but characters are limited to
# what real URLs use.
_GIT_URL_RE = re.compile(
    r"^(?:git\+)?https?://"
    r"[A-Za-z0-9._/:@~%-]{5,512}$"
)

# Banned arg patterns (each is checked against every entry of extra_args).
# Lifted from openclaw + a few extras for pip/uvx flags. The intent: a
# user cannot smuggle a script-running, prefix-overriding, or
# arbitrary-shell flag through ``extra_args`` and bypass the safety
# defaults.
_BANNED_ARG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^--prefix(=|$)"),
    re.compile(r"^--global(-option)?(=|$)"),
    re.compile(r"^--install-option(=|$)"),
    re.compile(r"^--script(-shell)?(=|$)"),
    re.compile(r"^--unsafe-perm"),
    re.compile(r"^--no-ignore-scripts"),
    re.compile(r"^--ignore-scripts=false"),
    re.compile(r"^--target($|=)"),       # we set this ourselves for pip
    re.compile(r"^-t($|=)"),             # short form of --target
    re.compile(r"^--user(=false)?$"),    # we set --user ourselves
    re.compile(r"^--root($|=)"),         # root install bypass
    re.compile(r"^--build-option"),
    re.compile(r"\s"),                   # whitespace = arg splitting attempt
    re.compile(r"[;|&`$<>]"),            # shell metachars
)

# Supported package managers. ``git_npm`` / ``git_pip`` are sub-types
# that use the same underlying binary but with a git URL spec instead of
# a registry name; they get separate validation paths.
_VALID_MANAGERS = frozenset({"npm", "pip", "uvx", "git_npm", "git_pip"})

_DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_TIMEOUT_SECONDS = 600.0
_TAIL_LINES = 30


# -- Public types -------------------------------------------------------------

class InstallArgError(ValueError):
    """Raised when validation rejects an install request.

    Use a dedicated subclass so callers can distinguish "operator passed
    something the tool refuses" from "subprocess failed at runtime"
    without inspecting message strings.
    """


@dataclass(slots=True)
class InstallRequest:
    """Validated payload for one install call. Construct via
    :func:`validate_install_args` so the regex / timeout checks always run.
    """

    package_manager: str
    package: str
    extra_args: tuple[str, ...] = ()
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    allow_scripts: bool = False


@dataclass(slots=True)
class InstallResult:
    """Outcome of a single install attempt.

    ``stdout_tail`` and ``stderr_tail`` are bounded (last :data:`_TAIL_LINES`
    lines each) so they're safe to dump into an IM message. ``error`` is
    populated only on failure; success cases set it to ``None``.
    """

    ok: bool
    package_manager: str
    package: str
    command: tuple[str, ...]
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_ms: int = 0
    exit_code: Optional[int] = None
    error: Optional[str] = None


@dataclass(slots=True)
class ListedPackage:
    """One entry returned by :func:`list_installed`."""

    package_manager: str
    name: str
    version: str = ""
    location: str = ""


@dataclass(slots=True)
class ListResult:
    """Outcome of a list-installed call.

    ``ok=False`` when the package manager itself errored (binary
    missing, registry unreachable, malformed JSON output). The
    individual entries are still in ``packages`` if any were parsed
    before the failure.
    """

    ok: bool
    package_manager: str
    packages: list[ListedPackage] = field(default_factory=list)
    error: Optional[str] = None


# -- Validation ---------------------------------------------------------------

def validate_install_args(args: dict) -> InstallRequest:
    """Convert a tool-call args dict into a validated :class:`InstallRequest`.

    Raises :class:`InstallArgError` on any reject path with a message
    suitable for echoing back to the operator. The same function is used
    by the IM tool and the REST API so the wire-level surface is
    identical across both entry points.
    """
    args = args or {}

    raw_manager = args.get("package_manager")
    if not isinstance(raw_manager, str) or not raw_manager.strip():
        raise InstallArgError(
            "missing required argument 'package_manager' (one of: "
            f"{sorted(_VALID_MANAGERS)})"
        )
    manager = raw_manager.strip().lower()
    if manager not in _VALID_MANAGERS:
        raise InstallArgError(
            f"package_manager must be one of {sorted(_VALID_MANAGERS)},"
            f" got {raw_manager!r}"
        )

    raw_package = args.get("package")
    if not isinstance(raw_package, str) or not raw_package.strip():
        raise InstallArgError("missing required argument 'package'")
    package = raw_package.strip()

    if manager in ("npm", "pip", "uvx"):
        if not _PKG_NAME_RE.match(package):
            raise InstallArgError(
                f"package name {package!r} is not a valid registry"
                " specifier; expected a name like 'foo' / '@org/foo' /"
                " 'foo@1.2.3', no shell metachars or whitespace"
            )
    else:  # git_npm or git_pip
        if not _GIT_URL_RE.match(package):
            raise InstallArgError(
                f"package {package!r} is not a valid git URL; expected"
                " https://... or git+https://... (no ssh, no shell"
                " metachars)"
            )

    raw_args = args.get("extra_args") or []
    if not isinstance(raw_args, list):
        raise InstallArgError("'extra_args' must be a list of strings")
    extra: list[str] = []
    for entry in raw_args:
        if not isinstance(entry, str):
            raise InstallArgError(
                f"every extra_args entry must be a string, got {type(entry).__name__}"
            )
        for pattern in _BANNED_ARG_PATTERNS:
            if pattern.search(entry):
                raise InstallArgError(
                    f"extra_args entry {entry!r} is blocked: matches the"
                    " banned-args denylist (script-running / prefix-override"
                    " / shell-metachar). The install tool sets safety flags"
                    " itself; do not override them."
                )
        extra.append(entry)

    raw_timeout = args.get("timeout_seconds")
    if raw_timeout is None:
        timeout = _DEFAULT_TIMEOUT_SECONDS
    else:
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise InstallArgError(
                f"timeout_seconds must be a number, got {raw_timeout!r}"
            ) from exc
        if timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
            raise InstallArgError(
                f"timeout_seconds must be in (0, {_MAX_TIMEOUT_SECONDS:.0f}],"
                f" got {timeout}"
            )

    allow_scripts = bool(args.get("allow_scripts") or False)

    return InstallRequest(
        package_manager=manager,
        package=package,
        extra_args=tuple(extra),
        timeout_seconds=timeout,
        allow_scripts=allow_scripts,
    )


# -- Command builders ---------------------------------------------------------

def _build_install_command(req: InstallRequest) -> tuple[str, ...]:
    """Compose the actual argv for the requested package manager.

    Each branch resolves the binary path via :func:`shutil.which` *not*
    here — we let the subprocess machinery do the lookup so missing
    binaries surface as a clean FileNotFoundError instead of an error
    string we have to assemble.

    npm side gets the openclaw safety set: ``--ignore-scripts``,
    ``--no-audit``, ``--no-fund``, ``--legacy-peer-deps``. These can be
    overridden only by setting ``allow_scripts=True``, which strips
    ``--ignore-scripts`` (the rest stay).
    """
    pkg = req.package
    extra = list(req.extra_args)
    if req.package_manager == "npm":
        cmd: list[str] = ["npm", "install", "-g"]
        if not req.allow_scripts:
            cmd.append("--ignore-scripts")
        cmd.extend(["--no-audit", "--no-fund", "--legacy-peer-deps"])
        cmd.extend(extra)
        cmd.append(pkg)
        return tuple(cmd)

    if req.package_manager == "git_npm":
        cmd = ["npm", "install", "-g"]
        if not req.allow_scripts:
            cmd.append("--ignore-scripts")
        cmd.extend(["--no-audit", "--no-fund", "--legacy-peer-deps"])
        cmd.extend(extra)
        # npm accepts ``git+https://...`` directly. If the user wrote
        # bare ``https://...github.com/...``, prepend ``git+`` so npm
        # recognises it as a git source rather than a tarball URL.
        spec = pkg if pkg.startswith("git+") else f"git+{pkg}"
        cmd.append(spec)
        return tuple(cmd)

    if req.package_manager == "pip":
        # ``--user`` honours PYTHONUSERBASE so binaries land in
        # /app/.packages/pip/bin (which the Dockerfile's PATH already
        # exposes). ``--no-input`` stops pip from blocking on a TTY
        # prompt when the registry asks for credentials.
        cmd = ["pip", "install", "--user", "--upgrade", "--no-input"]
        cmd.extend(extra)
        cmd.append(pkg)
        return tuple(cmd)

    if req.package_manager == "git_pip":
        cmd = ["pip", "install", "--user", "--upgrade", "--no-input"]
        cmd.extend(extra)
        spec = pkg if pkg.startswith("git+") else f"git+{pkg}"
        cmd.append(spec)
        return tuple(cmd)

    if req.package_manager == "uvx":
        # ``uv tool install`` creates an isolated venv per tool — exactly
        # what we want for sandboxed Python MCP servers. The bin link
        # lands in /app/.packages/pip/bin via UV_TOOL_BIN_DIR (set by
        # the caller, see :func:`install_package`).
        cmd = ["uv", "tool", "install"]
        cmd.extend(extra)
        cmd.append(pkg)
        return tuple(cmd)

    # validate_install_args() should have rejected this already, but
    # keep the runtime guard so the function is total.
    raise InstallArgError(
        f"unknown package_manager {req.package_manager!r}"
    )


# -- Logging helpers ----------------------------------------------------------

def _resolve_log_path(workspace_dir: Optional[Path]) -> Optional[Path]:
    if workspace_dir is None:
        return None
    log_dir = workspace_dir / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 — never block install on log dir
        logger.warning("[mcp.install] cannot create log dir {}: {}", log_dir, exc)
        return None
    return log_dir / "mcp-install.log"


def _append_install_log(
    log_path: Optional[Path],
    *,
    req: InstallRequest,
    cmd: tuple[str, ...],
    stdout: str,
    stderr: str,
    exit_code: Optional[int],
    duration_ms: int,
) -> None:
    """Best-effort append-only log for postmortem.

    Failure to write the log is a warning, never an error — the install
    itself may have succeeded and the operator should still see the
    success/failure shape of the result.
    """
    if log_path is None:
        return
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as fh:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
            fh.write(f"\n===== {ts} {req.package_manager} {req.package} =====\n")
            fh.write(f"$ {' '.join(cmd)}\n")
            fh.write(f"--- stdout ({len(stdout)} chars) ---\n")
            fh.write(stdout)
            if not stdout.endswith("\n"):
                fh.write("\n")
            fh.write(f"--- stderr ({len(stderr)} chars) ---\n")
            fh.write(stderr)
            if not stderr.endswith("\n"):
                fh.write("\n")
            fh.write(f"--- exit={exit_code} duration={duration_ms}ms ---\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.install] log append failed at {}: {}", log_path, exc)


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    if not text:
        return ""
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


# -- Public install API ------------------------------------------------------

async def install_package(
    req: InstallRequest,
    *,
    workspace_dir: Optional[Path] = None,
) -> InstallResult:
    """Run the install subprocess for ``req`` and capture the outcome.

    Always returns :class:`InstallResult`; raises only on programmer
    errors (e.g. bad request type). Subprocess failures, timeouts, and
    missing-binary cases all flow back through the result's
    ``ok=False`` + ``error`` channel so the calling tool can render a
    clean message to the operator.
    """
    if not isinstance(req, InstallRequest):
        raise TypeError(
            f"install_package requires InstallRequest, got {type(req).__name__}"
        )

    cmd = _build_install_command(req)
    log_path = _resolve_log_path(workspace_dir)

    # uvx routes its bin links through UV_TOOL_BIN_DIR; default it to
    # /app/.packages/pip/bin so the operator's PATH (set in the
    # Dockerfile) finds the resulting executable. Caller can still
    # override via os.environ if they really want a different layout.
    import os as _os
    env = dict(_os.environ)
    env.setdefault("UV_TOOL_BIN_DIR", "/app/.packages/pip/bin")
    env.setdefault("UV_CACHE_DIR", "/app/.packages/uv-cache")
    env.setdefault("UV_TOOL_DIR", "/app/.packages/uv/tools")
    # v1.1.1 — pip download cache + uv-managed Python interpreters also
    # need to land under the single /app/.packages/ mount, otherwise they
    # fall back to HOME-relative defaults (~/.cache/pip,
    # ~/.local/share/uv/python) which ``zlagent`` (uid 10001) cannot
    # write. Dockerfile sets these globally; the setdefault is
    # belt-and-braces if the image is ever rebuilt without them.
    env.setdefault("PIP_CACHE_DIR", "/app/.packages/pip-cache")
    env.setdefault("UV_PYTHON_INSTALL_DIR", "/app/.packages/uv/python")
    # Doubled-up safety: even if --ignore-scripts is somehow stripped at
    # the argv layer, the env var keeps npm honest. ``allow_scripts``
    # explicitly disables both.
    if req.allow_scripts:
        env.pop("NPM_CONFIG_IGNORE_SCRIPTS", None)
    else:
        env["NPM_CONFIG_IGNORE_SCRIPTS"] = "true"
    # Quieten npm output so the IM-friendly tail isn't dominated by
    # progress bars. ``--loglevel=error`` is the closest equivalent
    # available everywhere.
    env.setdefault("NPM_CONFIG_LOGLEVEL", "error")
    env.setdefault("NPM_CONFIG_PROGRESS", "false")
    env.setdefault("COREPACK_ENABLE_DOWNLOAD_PROMPT", "0")

    start = time.monotonic()
    stdout = ""
    stderr = ""
    exit_code: Optional[int] = None
    error: Optional[str] = None

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        # Most common at first deploy: ``npm``/``uv``/``pip`` itself
        # missing on PATH. Surface the binary name so the operator
        # knows what to install on the host.
        duration_ms = int((time.monotonic() - start) * 1000)
        binary = cmd[0] if cmd else "?"
        error = (
            f"binary {binary!r} not found on PATH. The install tool"
            " expects npm, pip, and uv to be available; rebuild the"
            f" container or install {binary} manually. Underlying"
            f" error: {exc}"
        )
        return InstallResult(
            ok=False, package_manager=req.package_manager,
            package=req.package, command=cmd,
            stdout_tail="", stderr_tail="",
            duration_ms=duration_ms, exit_code=None, error=error,
        )
    except Exception as exc:  # noqa: BLE001 — keep loop alive
        duration_ms = int((time.monotonic() - start) * 1000)
        return InstallResult(
            ok=False, package_manager=req.package_manager,
            package=req.package, command=cmd,
            stdout_tail="", stderr_tail="",
            duration_ms=duration_ms, exit_code=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=req.timeout_seconds,
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        duration_ms = int((time.monotonic() - start) * 1000)
        error = (
            f"install timed out after {req.timeout_seconds:.0f}s"
            " (subprocess killed); raise timeout_seconds for slower"
            " networks or pre-warm the registry"
        )
        _append_install_log(
            log_path, req=req, cmd=cmd, stdout="", stderr=error,
            exit_code=None, duration_ms=duration_ms,
        )
        return InstallResult(
            ok=False, package_manager=req.package_manager,
            package=req.package, command=cmd,
            stdout_tail="", stderr_tail=error,
            duration_ms=duration_ms, exit_code=None, error=error,
        )

    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    exit_code = proc.returncode
    duration_ms = int((time.monotonic() - start) * 1000)

    _append_install_log(
        log_path, req=req, cmd=cmd, stdout=stdout, stderr=stderr,
        exit_code=exit_code, duration_ms=duration_ms,
    )

    ok = exit_code == 0
    if not ok:
        error = (
            f"{req.package_manager} install exited with code {exit_code}"
            " — see stderr_tail (and workspace/logs/mcp-install.log for"
            " the full transcript)"
        )

    return InstallResult(
        ok=ok, package_manager=req.package_manager,
        package=req.package, command=cmd,
        stdout_tail=_tail(stdout), stderr_tail=_tail(stderr),
        duration_ms=duration_ms, exit_code=exit_code, error=error,
    )


# -- List-installed -----------------------------------------------------------

async def list_installed(package_manager: str) -> ListResult:
    """Enumerate the currently-installed packages for one manager.

    Read-only. Used by ``mcp_manage(action='installed')`` to surface
    what's been added since deploy. Each manager has its own
    "list packages" command and JSON shape — we parse just enough to
    populate :class:`ListedPackage` and skip everything else.
    """
    manager = (package_manager or "").strip().lower()
    if manager not in {"npm", "pip", "uvx"}:
        return ListResult(
            ok=False, package_manager=manager,
            error=(
                "list is only supported for npm / pip / uvx; git_npm /"
                " git_pip installs surface under their underlying manager"
            ),
        )

    # Resolving the binary up-front gives us a friendlier error than
    # asyncio.create_subprocess_exec's FileNotFoundError.
    binary = {"npm": "npm", "pip": "pip", "uvx": "uv"}[manager]
    if shutil.which(binary) is None:
        return ListResult(
            ok=False, package_manager=manager,
            error=f"{binary!r} not found on PATH",
        )

    if manager == "npm":
        cmd = ("npm", "ls", "-g", "--depth=0", "--json")
    elif manager == "pip":
        cmd = ("pip", "list", "--user", "--format=json")
    else:  # uvx
        cmd = ("uv", "tool", "list")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=20.0,
        )
    except asyncio.TimeoutError:
        return ListResult(
            ok=False, package_manager=manager,
            error=f"{binary} list timed out after 20s",
        )
    except Exception as exc:  # noqa: BLE001
        return ListResult(
            ok=False, package_manager=manager,
            error=f"{type(exc).__name__}: {exc}",
        )

    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""

    if manager in ("npm", "pip"):
        return _parse_json_list(manager, stdout, stderr, proc.returncode)
    return _parse_uv_tool_list(stdout, stderr, proc.returncode)


def _parse_json_list(
    manager: str, stdout: str, stderr: str, exit_code: Optional[int],
) -> ListResult:
    """Parse npm/pip's JSON output into :class:`ListedPackage` entries.

    npm exits with code 1 when there are unmet peer deps even though
    the JSON itself is valid — we tolerate that and treat any parsable
    output as ``ok=True``.
    """
    import json
    if not stdout.strip():
        return ListResult(
            ok=False, package_manager=manager,
            error=stderr.strip() or f"{manager} list produced no output",
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return ListResult(
            ok=False, package_manager=manager,
            error=f"could not parse {manager} JSON: {exc}",
        )

    out: list[ListedPackage] = []
    if manager == "npm":
        deps = payload.get("dependencies") or {}
        if isinstance(deps, dict):
            for name, info in deps.items():
                version = ""
                if isinstance(info, dict):
                    version = str(info.get("version") or "")
                out.append(ListedPackage(
                    package_manager="npm", name=str(name), version=version,
                    location=str(payload.get("name") or ""),
                ))
    else:  # pip
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                out.append(ListedPackage(
                    package_manager="pip",
                    name=str(entry.get("name") or ""),
                    version=str(entry.get("version") or ""),
                    location=str(entry.get("location") or ""),
                ))

    return ListResult(ok=True, package_manager=manager, packages=out)


def _parse_uv_tool_list(
    stdout: str, stderr: str, exit_code: Optional[int],
) -> ListResult:
    """Parse ``uv tool list`` plain-text output.

    ``uv`` does not emit JSON for ``tool list`` as of the current
    release, so we walk lines looking for entries of the shape
    ``<name> v<version>`` (with following ``- /path/to/bin`` lines we
    ignore). Any malformed line is silently skipped.
    """
    if exit_code not in (0, None) and not stdout.strip():
        return ListResult(
            ok=False, package_manager="uvx",
            error=stderr.strip() or f"uv tool list exited with code {exit_code}",
        )
    out: list[ListedPackage] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("-"):
            continue
        # ``foo v1.2.3`` or ``foo 1.2.3``
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        version = ""
        if len(parts) > 1:
            tail = parts[1]
            version = tail.lstrip("v")
        out.append(ListedPackage(
            package_manager="uvx", name=name, version=version,
        ))
    return ListResult(ok=True, package_manager="uvx", packages=out)


__all__ = [
    "InstallArgError",
    "InstallRequest",
    "InstallResult",
    "ListResult",
    "ListedPackage",
    "install_package",
    "list_installed",
    "validate_install_args",
]
