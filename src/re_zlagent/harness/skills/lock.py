"""Auditable provenance lock for remotely installed Skills."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .installer import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    SkillInstallError,
    SkillInstallErrorCode,
)


SKILL_LOCK_SCHEMA_VERSION = 1
MAX_SKILL_LOCK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SkillLockEntry:
    """Pinned source and validated content identity for one installed Skill."""

    skill_id: str
    name: str
    version: str
    source: str
    repository: str
    requested_ref: str
    resolved_commit: str
    subpath: str
    digest: str
    file_count: int
    total_bytes: int
    scan_verdict: str
    finding_ids: tuple[str, ...] = ()
    installed_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_ids", tuple(self.finding_ids))
        if not self.installed_at:
            object.__setattr__(
                self,
                "installed_at",
                datetime.now(UTC).isoformat(),
            )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["finding_ids"] = list(self.finding_ids)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillLockEntry:
        return cls(
            skill_id=str(data["skill_id"]),
            name=str(data["name"]),
            version=str(data["version"]),
            source=str(data["source"]),
            repository=str(data["repository"]),
            requested_ref=str(data["requested_ref"]),
            resolved_commit=str(data["resolved_commit"]),
            subpath=str(data.get("subpath") or ""),
            digest=str(data["digest"]),
            file_count=int(data["file_count"]),
            total_bytes=int(data["total_bytes"]),
            scan_verdict=str(data["scan_verdict"]),
            finding_ids=tuple(str(item) for item in data.get("finding_ids", ())),
            installed_at=str(data["installed_at"]),
        )


class SkillLockfile:
    """Read and atomically update ``skills.lock.json`` in a managed root."""

    def __init__(self, managed_root: Path | str) -> None:
        self._managed_root = Path(managed_root).resolve()
        self._path = self._managed_root / "skills.lock.json"

    @property
    def path(self) -> Path:
        return self._path

    def get(self, skill_id: str) -> SkillLockEntry | None:
        return self._load().get(skill_id)

    def record(self, entry: SkillLockEntry) -> None:
        entries = self._load()
        existing = entries.get(entry.skill_id)
        if existing is not None and existing.source != entry.source:
            raise SkillInstallError(
                SkillInstallErrorCode.CONFLICT,
                "installed Skill is already locked to a different source",
                details={
                    "skill_id": entry.skill_id,
                    "locked_source": existing.source,
                    "requested_source": entry.source,
                },
            )
        entries[entry.skill_id] = entry
        payload = {
            "schema_version": SKILL_LOCK_SCHEMA_VERSION,
            "skills": {
                key: value.to_dict()
                for key, value in sorted(entries.items())
            },
        }
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".skills.lock.",
                suffix=".tmp",
                dir=self._managed_root,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except OSError as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.IO_ERROR,
                f"Skill lockfile update failed: {exc}",
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _load(self) -> dict[str, SkillLockEntry]:
        if not self._path.exists():
            return {}
        if self._path.is_symlink() or not self._path.is_file():
            raise SkillInstallError(
                SkillInstallErrorCode.UNSAFE_PATH,
                "Skill lockfile must be a regular file",
            )
        try:
            if self._path.stat().st_size > MAX_SKILL_LOCK_BYTES:
                raise ValueError("lockfile exceeds byte limit")
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != SKILL_LOCK_SCHEMA_VERSION:
                raise ValueError("unsupported schema_version")
            skills = raw.get("skills")
            if not isinstance(skills, dict):
                raise ValueError("skills must be an object")
            entries: dict[str, SkillLockEntry] = {}
            for key, value in skills.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise ValueError("Skill lock entries must be named objects")
                entry = SkillLockEntry.from_dict(value)
                if key != entry.skill_id:
                    raise ValueError("Skill lock key does not match skill_id")
                entries[key] = entry
            return entries
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                f"invalid Skill lockfile: {exc}",
            ) from exc
        except OSError as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.IO_ERROR,
                f"Skill lockfile read failed: {exc}",
            ) from exc


def installed_tree_digest(
    root: Path | str,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> str:
    """Hash a regular-file tree without following links."""

    base = Path(root)
    if base.is_symlink() or not base.is_dir():
        raise SkillInstallError(
            SkillInstallErrorCode.UNSAFE_PATH,
            "installed Skill target must be a regular directory",
        )
    items: list[dict[str, Any]] = []
    total_bytes = 0
    stack = [base]
    try:
        while stack:
            folder = stack.pop()
            for entry in sorted(os.scandir(folder), key=lambda item: item.name):
                path = Path(entry.path)
                relative_path = path.relative_to(base)
                relative = relative_path.as_posix()
                if len(relative_path.parts) > max_depth:
                    raise SkillInstallError(
                        SkillInstallErrorCode.LIMIT_EXCEEDED,
                        f"installed Skill exceeds depth limit: {relative}",
                    )
                if entry.is_symlink():
                    raise SkillInstallError(
                        SkillInstallErrorCode.UNSAFE_PATH,
                        f"installed Skill contains symlink: {relative}",
                    )
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise SkillInstallError(
                        SkillInstallErrorCode.UNSAFE_PATH,
                        f"installed Skill contains non-regular file: {relative}",
                    )
                data = path.read_bytes()
                if len(data) > max_file_bytes:
                    raise SkillInstallError(
                        SkillInstallErrorCode.LIMIT_EXCEEDED,
                        f"installed Skill file exceeds size limit: {relative}",
                    )
                total_bytes += len(data)
                if total_bytes > max_total_bytes:
                    raise SkillInstallError(
                        SkillInstallErrorCode.LIMIT_EXCEEDED,
                        "installed Skill exceeds total byte limit",
                    )
                items.append(
                    {
                        "path": relative,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                    }
                )
                if len(items) > max_files:
                    raise SkillInstallError(
                        SkillInstallErrorCode.LIMIT_EXCEEDED,
                        "installed Skill exceeds file count limit",
                    )
    except SkillInstallError:
        raise
    except OSError as exc:
        raise SkillInstallError(
            SkillInstallErrorCode.IO_ERROR,
            f"installed Skill read failed: {exc}",
        ) from exc
    canonical = json.dumps(
        sorted(items, key=lambda item: str(item["path"])),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
