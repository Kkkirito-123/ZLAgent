"""Filesystem-backed read-only skill loader."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .guard import SkillGuard, SkillScanResult
from .types import SkillFormat, SkillManifest

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class SkillLoadError(Exception):
    """Raised when the skills directory contains invalid data."""


class FileSystemSkillLoader:
    """Load Hermes and legacy skill manifests from a workspace directory."""

    def __init__(
        self,
        skills_dir: Path | str,
        *,
        max_depth: int = 3,
        guard: SkillGuard | None = None,
    ) -> None:
        self._root = Path(skills_dir).resolve()
        self._max_depth = max(1, max_depth)
        self._guard = guard or SkillGuard()
        self._skills: dict[str, SkillManifest] = {}
        if self._root.exists() and not self._root.is_dir():
            raise SkillLoadError(f"skills path is not a directory: {self._root}")

    def load(self) -> dict[str, SkillManifest]:
        self._skills.clear()
        if not self._root.exists():
            return {}
        self._scan(self._root, depth=0)
        return dict(self._skills)

    def list(self) -> tuple[SkillManifest, ...]:
        return tuple(self._skills.values())

    def get(self, skill_id: str) -> SkillManifest | None:
        return self._skills.get(skill_id)

    def read_body(self, skill_id: str) -> str | None:
        manifest = self._skills.get(skill_id)
        if manifest is None or manifest.body_path is None:
            return None
        body_path = self._resolve_inside_root(manifest.body_path)
        raw = body_path.read_text(encoding="utf-8")
        if manifest.format is SkillFormat.HERMES:
            match = _FRONTMATTER_RE.match(raw)
            return (match.group(2) if match else raw).strip()
        return raw.strip()

    def scan_body(self, skill_id: str) -> SkillScanResult | None:
        body = self.read_body(skill_id)
        if body is None:
            return None
        return self._guard.scan(body)

    def _scan(self, folder: Path, *, depth: int) -> None:
        folder = self._resolve_inside_root(folder)
        manifest = self._load_one(folder)
        if manifest is not None:
            if manifest.id in self._skills:
                raise SkillLoadError(f"duplicate skill id: {manifest.id}")
            self._skills[manifest.id] = manifest
            return
        if depth >= self._max_depth:
            return
        for child in sorted(folder.iterdir()):
            if child.is_dir():
                self._scan(child, depth=depth + 1)

    def _load_one(self, folder: Path) -> SkillManifest | None:
        skill_md = folder / "SKILL.md"
        if skill_md.is_file():
            return self._load_hermes(skill_md, folder)
        skill_yaml = folder / "skill.yaml"
        instructions = folder / "instructions.md"
        if skill_yaml.is_file() and instructions.is_file():
            return self._load_legacy(skill_yaml, instructions, folder)
        return None

    def _load_hermes(self, skill_md: Path, folder: Path) -> SkillManifest:
        skill_md = self._resolve_inside_root(skill_md)
        raw = skill_md.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(raw)
        meta = _parse_simple_metadata(match.group(1) if match else "")
        skill_id = str(meta.get("id") or folder.name)
        return SkillManifest(
            id=skill_id,
            name=str(meta.get("name") or skill_id),
            description=str(meta.get("description") or ""),
            version=str(meta.get("version") or "0.1.0"),
            tags=tuple(meta.get("tags") or ()),
            triggers=tuple(meta.get("triggers") or ()),
            format=SkillFormat.HERMES,
            root=folder,
            body_path=skill_md,
            metadata=meta,
        )

    def _load_legacy(
        self,
        skill_yaml: Path,
        instructions: Path,
        folder: Path,
    ) -> SkillManifest:
        skill_yaml = self._resolve_inside_root(skill_yaml)
        instructions = self._resolve_inside_root(instructions)
        meta = _parse_simple_metadata(skill_yaml.read_text(encoding="utf-8"))
        skill_id = str(meta.get("id") or meta.get("name") or folder.name)
        return SkillManifest(
            id=skill_id,
            name=str(meta.get("name") or skill_id),
            description=str(meta.get("description") or ""),
            version=str(meta.get("version") or "0.1.0"),
            tags=tuple(meta.get("tags") or ()),
            triggers=tuple(meta.get("triggers") or ()),
            format=SkillFormat.LEGACY,
            root=folder,
            body_path=instructions,
            metadata=meta,
        )

    def _resolve_inside_root(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise SkillLoadError(f"path escapes skills root: {path}") from exc
        return resolved


def _parse_simple_metadata(text: str) -> dict[str, Any]:
    """Parse a small YAML-like subset without external dependencies."""

    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if current_list_key and line.startswith("- "):
            meta.setdefault(current_list_key, []).append(_strip_quotes(line[2:].strip()))
            continue
        current_list_key = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value == "":
            meta[key] = []
            current_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            body = value[1:-1].strip()
            meta[key] = [
                _strip_quotes(part.strip())
                for part in body.split(",")
                if part.strip()
            ]
        else:
            meta[key] = _strip_quotes(value)
    return meta


def _strip_quotes(value: str) -> str:
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value
