"""Parse + validate ``plugin.json`` manifest files.

We use JSON not YAML to avoid pulling in PyYAML or hand-rolling a
parser — the manifest is structured config and humans editing it can
deal with the slightly noisier syntax.

Manifest format::

    {
      "id": "my-plugin",
      "version": "0.1.0",
      "description": "Optional one-liner",
      "kinds": ["tools", "skills"],
      "permissions": {
        "tools": ["read_url"],
        "filesystem": {"writable_paths": ["workspace/my-plugin/"]}
      }
    }

Required: ``id`` (slug ``[a-z][a-z0-9_-]{0,63}``), ``version``
(semver-like ``\\d+(\\.\\d+){0,3}``). Everything else optional.
``kinds`` entries must match a :class:`PluginKind` value or are
silently dropped from the typed tuple but kept in ``raw`` for audit.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import PluginKind


class ManifestError(ValueError):
    """Raised when a ``plugin.json`` is malformed or missing required fields."""


_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}([+-][A-Za-z0-9.\-]+)?$")


@dataclass(slots=True)
class PluginManifest:
    id: str
    version: str
    description: str = ""
    kinds: tuple[PluginKind, ...] = ()
    permissions: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "kinds": [k.value for k in self.kinds],
            "permissions": dict(self.permissions),
        }


def parse_manifest_dict(data: Any) -> PluginManifest:
    """Validate a parsed manifest mapping and return a :class:`PluginManifest`."""
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object at top level")

    plugin_id = data.get("id")
    if not isinstance(plugin_id, str) or not _ID_RE.match(plugin_id):
        raise ManifestError(
            f"invalid plugin id {plugin_id!r}; must match [a-z][a-z0-9_-]{{0,63}}"
        )

    version = data.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ManifestError(
            f"invalid plugin version {version!r}; expected e.g. '0.1.0'"
        )

    description = data.get("description", "")
    if not isinstance(description, str):
        raise ManifestError("description must be a string")

    raw_kinds = data.get("kinds", [])
    if not isinstance(raw_kinds, list):
        raise ManifestError("kinds must be a list of strings")
    typed_kinds: list[PluginKind] = []
    for k in raw_kinds:
        if not isinstance(k, str):
            raise ManifestError(f"kinds entries must be strings; got {k!r}")
        try:
            typed_kinds.append(PluginKind(k))
        except ValueError:
            # Unknown kind — keep in raw, drop from typed tuple. Loader
            # may surface a warning but we don't reject the manifest
            # outright.
            continue

    permissions = data.get("permissions", {})
    if not isinstance(permissions, dict):
        raise ManifestError("permissions must be an object")

    return PluginManifest(
        id=plugin_id,
        version=version,
        description=description,
        kinds=tuple(typed_kinds),
        permissions=permissions,
        raw=dict(data),
    )


def load_manifest(path: Path) -> PluginManifest:
    """Read ``path`` (a ``plugin.json``) and validate it.

    Raises :class:`ManifestError` on missing file, malformed JSON, or
    invalid contents. Callers should treat that exception as
    "skip this plugin, surface error in /api/plugins".
    """
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"manifest not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in {path.name}: {exc.msg} (line {exc.lineno})") from exc
    return parse_manifest_dict(data)


__all__ = ["PluginManifest", "ManifestError", "load_manifest", "parse_manifest_dict"]
