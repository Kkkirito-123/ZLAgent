"""Controlled local skill installation lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from re_zlagent.harness.sandbox import WorkspacePathError, WorkspacePathPolicy

from .guard import (
    SkillFinding,
    SkillGuard,
    SkillScanResult,
    SkillScanVerdict,
)
from .loader import FileSystemSkillLoader, SkillLoadError
from .types import SkillManifest

DEFAULT_MAX_FILES = 256
DEFAULT_MAX_FILE_BYTES = 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_DEPTH = 8

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class SkillInstallErrorCode(str, Enum):
    """Stable failure classes for local skill installation."""

    INVALID_ID = "invalid_id"
    UNSAFE_PATH = "unsafe_path"
    SOURCE_NOT_FOUND = "source_not_found"
    INVALID_PACKAGE = "invalid_package"
    LIMIT_EXCEEDED = "limit_exceeded"
    DANGEROUS_CONTENT = "dangerous_content"
    CONFLICT = "conflict"
    IO_ERROR = "io_error"


class SkillInstallError(ValueError):
    """Reject an installation before an unsafe or ambiguous write."""

    def __init__(
        self,
        code: SkillInstallErrorCode,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class SkillInstallStatus(str, Enum):
    """Outcome of one idempotent install attempt."""

    INSTALLED = "installed"
    ALREADY_INSTALLED = "already_installed"


@dataclass(frozen=True, slots=True)
class SkillInstallPreview:
    """Validated, non-mutating view of a candidate package."""

    manifest: SkillManifest
    source_path: str
    target_ref: str
    source_digest: str
    file_count: int
    total_bytes: int
    scan_result: SkillScanResult
    scanned_text_files: int
    already_installed: bool = False


@dataclass(frozen=True, slots=True)
class SkillInstallResult:
    """Structured installation outcome for tool evidence."""

    status: SkillInstallStatus
    preview: SkillInstallPreview
    inventory_refreshed: bool = True
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    relative_path: str
    data: bytes = field(repr=False)
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data)


def validate_skill_id(skill_id: object) -> str:
    """Return a canonical bounded skill id or raise a typed error."""

    value = str(skill_id or "").strip()
    if not _SKILL_ID_RE.fullmatch(value):
        raise SkillInstallError(
            SkillInstallErrorCode.INVALID_ID,
            "skill_id must be 1-64 lowercase letters, digits, '.', '_' or '-'",
            details={"skill_id": value},
        )
    return value


class LocalSkillInstaller:
    """Install one validated package from a controlled local import root.

    The installer never downloads, executes, updates, or deletes a skill. A
    different package at the same skill id is a conflict, while byte-identical
    content is an idempotent success suitable for outbox replay.
    """

    def __init__(
        self,
        source_root: Path | str,
        managed_root: Path | str,
        *,
        loader: FileSystemSkillLoader | None = None,
        guard: SkillGuard | None = None,
        max_files: int = DEFAULT_MAX_FILES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        source_input = Path(source_root).expanduser()
        managed_input = Path(managed_root).expanduser()
        if source_input.is_symlink() or managed_input.is_symlink():
            raise SkillInstallError(
                SkillInstallErrorCode.UNSAFE_PATH,
                "skill source and managed roots must not be symlinks",
            )
        self._source_root = source_input.resolve()
        self._managed_root = managed_input.resolve()
        for label, root in (
            ("skill source", self._source_root),
            ("managed skill", self._managed_root),
        ):
            if not root.exists() or not root.is_dir():
                raise SkillInstallError(
                    SkillInstallErrorCode.SOURCE_NOT_FOUND,
                    f"{label} root must be an existing directory: {root}",
                )
        if _paths_overlap(self._source_root, self._managed_root):
            raise SkillInstallError(
                SkillInstallErrorCode.UNSAFE_PATH,
                "skill source and managed roots must be separate directories",
            )
        if min(max_files, max_file_bytes, max_total_bytes, max_depth) <= 0:
            raise ValueError("skill installation limits must be positive")
        self._source_policy = WorkspacePathPolicy(self._source_root)
        self._guard = guard or SkillGuard()
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes
        self._max_depth = max_depth
        self._loader = loader or FileSystemSkillLoader(
            self._managed_root,
            guard=self._guard,
        )
        if self._loader.root != self._managed_root:
            raise ValueError("skill loader root must match managed_root")

    @property
    def loader(self) -> FileSystemSkillLoader:
        """Return the managed loader refreshed after successful installs."""

        return self._loader

    def validate_source_reference(self, source_path: object) -> str:
        """Validate only the bounded relative path used in an install intent."""

        try:
            resolved = self._source_policy.resolve(source_path)
        except WorkspacePathError as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.UNSAFE_PATH,
                exc.message,
                details={"path_error": exc.code.value},
            ) from exc
        self._reject_symlink_chain(resolved.relative_path)
        return resolved.relative_path

    def preview(self, source_path: object, skill_id: object) -> SkillInstallPreview:
        """Validate a package without changing the managed root."""

        preview, _ = self._prepare(source_path, skill_id)
        return preview

    def install(self, source_path: object, skill_id: object) -> SkillInstallResult:
        """Install atomically, refusing to overwrite different content."""

        preview, snapshots = self._prepare(source_path, skill_id)
        target = self._managed_root / preview.manifest.id
        if preview.already_installed:
            return self._finish(
                SkillInstallStatus.ALREADY_INSTALLED,
                preview,
            )

        temporary = Path(
            tempfile.mkdtemp(
                prefix=(
                    f".{self._managed_root.name}."
                    f"{preview.manifest.id}.install-"
                ),
                dir=self._managed_root.parent,
            )
        )
        try:
            for snapshot in snapshots:
                destination = temporary / Path(snapshot.relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(snapshot.data)
            copied = self._snapshot_tree(temporary)
            if _tree_digest(copied) != preview.source_digest:
                raise SkillInstallError(
                    SkillInstallErrorCode.IO_ERROR,
                    "copied skill package digest does not match validated source",
                )
            if target.exists() or target.is_symlink():
                return self._resolve_install_race(preview, target)
            try:
                os.rename(temporary, target)
            except OSError as exc:
                if target.exists() or target.is_symlink():
                    return self._resolve_install_race(preview, target)
                raise SkillInstallError(
                    SkillInstallErrorCode.IO_ERROR,
                    f"skill install rename failed: {exc}",
                ) from exc
            return self._finish(SkillInstallStatus.INSTALLED, preview)
        except SkillInstallError:
            raise
        except OSError as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.IO_ERROR,
                f"skill install failed: {exc}",
            ) from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _prepare(
        self,
        source_path: object,
        skill_id: object,
    ) -> tuple[SkillInstallPreview, tuple[_FileSnapshot, ...]]:
        expected_id = validate_skill_id(skill_id)
        relative_source = self.validate_source_reference(source_path)
        source = self._source_root / Path(relative_source)
        if not source.exists() or not source.is_dir():
            raise SkillInstallError(
                SkillInstallErrorCode.SOURCE_NOT_FOUND,
                f"skill source directory not found: {relative_source}",
            )
        snapshots_before_manifest = self._snapshot_tree(source)
        manifest = self._load_root_manifest(source)
        snapshots = self._snapshot_tree(source)
        if _tree_digest(snapshots) != _tree_digest(snapshots_before_manifest):
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                "skill source changed during validation; retry from a stable package",
                details={"source_path": relative_source},
            )
        if manifest.id != expected_id:
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                "skill manifest id does not match requested skill_id",
                details={"manifest_id": manifest.id, "skill_id": expected_id},
            )
        scan_result, scanned_text_files = self._scan_snapshots(snapshots)
        if self._guard.should_block(scan_result):
            raise SkillInstallError(
                SkillInstallErrorCode.DANGEROUS_CONTENT,
                "skill package contains blocked dangerous instructions",
                details={
                    "verdict": scan_result.verdict.value,
                    "finding_ids": [item.pattern_id for item in scan_result.findings],
                },
            )
        source_digest = _tree_digest(snapshots)
        target = self._managed_root / expected_id
        already_installed = False
        if target.is_symlink():
            raise SkillInstallError(
                SkillInstallErrorCode.UNSAFE_PATH,
                f"managed skill target must not be a symlink: {expected_id}",
            )
        if target.exists():
            if not target.is_dir():
                raise SkillInstallError(
                    SkillInstallErrorCode.CONFLICT,
                    f"managed skill target is not a directory: {expected_id}",
                )
            installed_digest = _tree_digest(self._snapshot_tree(target))
            if installed_digest != source_digest:
                raise SkillInstallError(
                    SkillInstallErrorCode.CONFLICT,
                    f"different skill content is already installed: {expected_id}",
                    details={
                        "source_digest": source_digest,
                        "installed_digest": installed_digest,
                    },
                )
            already_installed = True
        return (
            SkillInstallPreview(
                manifest=manifest,
                source_path=relative_source,
                target_ref=f"skills/{expected_id}",
                source_digest=source_digest,
                file_count=len(snapshots),
                total_bytes=sum(item.size for item in snapshots),
                scan_result=scan_result,
                scanned_text_files=scanned_text_files,
                already_installed=already_installed,
            ),
            snapshots,
        )

    def _load_root_manifest(self, source: Path) -> SkillManifest:
        skill_md = source / "SKILL.md"
        skill_yaml = source / "skill.yaml"
        instructions = source / "instructions.md"
        has_hermes = skill_md.is_file()
        has_legacy_yaml = skill_yaml.is_file()
        has_legacy_instructions = instructions.is_file()
        if has_hermes and (has_legacy_yaml or has_legacy_instructions):
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                "skill package must not mix Hermes and legacy manifests",
            )
        if has_legacy_yaml != has_legacy_instructions:
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                "legacy skill package requires both skill.yaml and instructions.md",
            )
        try:
            loader = FileSystemSkillLoader(source, max_depth=1, guard=self._guard)
            skills = loader.load()
        except (OSError, UnicodeError, ValueError, SkillLoadError) as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                f"invalid skill package: {exc}",
            ) from exc
        root = source.resolve()
        manifests = [item for item in skills.values() if item.root == root]
        if len(manifests) != 1:
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                "skill package root must contain SKILL.md or skill.yaml plus instructions.md",
            )
        manifest = manifests[0]
        try:
            validate_skill_id(manifest.id)
        except SkillInstallError as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                f"invalid manifest skill id: {manifest.id}",
            ) from exc
        return manifest

    def _snapshot_tree(self, root: Path) -> tuple[_FileSnapshot, ...]:
        snapshots: list[_FileSnapshot] = []
        total_bytes = 0
        stack = [root]
        try:
            while stack:
                folder = stack.pop()
                entries = sorted(os.scandir(folder), key=lambda item: item.name)
                for entry in entries:
                    path = Path(entry.path)
                    relative = path.relative_to(root)
                    if len(relative.parts) > self._max_depth:
                        raise SkillInstallError(
                            SkillInstallErrorCode.LIMIT_EXCEEDED,
                            f"skill package exceeds depth limit: {relative.as_posix()}",
                        )
                    if entry.is_symlink():
                        raise SkillInstallError(
                            SkillInstallErrorCode.UNSAFE_PATH,
                            f"skill package contains symlink: {relative.as_posix()}",
                        )
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        raise SkillInstallError(
                            SkillInstallErrorCode.UNSAFE_PATH,
                            f"skill package contains non-regular file: {relative.as_posix()}",
                        )
                    data = path.read_bytes()
                    if len(data) > self._max_file_bytes:
                        raise SkillInstallError(
                            SkillInstallErrorCode.LIMIT_EXCEEDED,
                            f"skill file exceeds size limit: {relative.as_posix()}",
                        )
                    total_bytes += len(data)
                    if total_bytes > self._max_total_bytes:
                        raise SkillInstallError(
                            SkillInstallErrorCode.LIMIT_EXCEEDED,
                            "skill package exceeds total byte limit",
                        )
                    snapshots.append(
                        _FileSnapshot(
                            relative_path=relative.as_posix(),
                            data=data,
                            sha256=hashlib.sha256(data).hexdigest(),
                        )
                    )
                    if len(snapshots) > self._max_files:
                        raise SkillInstallError(
                            SkillInstallErrorCode.LIMIT_EXCEEDED,
                            "skill package exceeds file count limit",
                        )
        except SkillInstallError:
            raise
        except OSError as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.IO_ERROR,
                f"skill package read failed: {exc}",
            ) from exc
        if not snapshots:
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                "skill package is empty",
            )
        return tuple(sorted(snapshots, key=lambda item: item.relative_path))

    def _scan_snapshots(
        self,
        snapshots: tuple[_FileSnapshot, ...],
    ) -> tuple[SkillScanResult, int]:
        findings: list[SkillFinding] = []
        scanned = 0
        for snapshot in snapshots:
            try:
                text = snapshot.data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            scanned += 1
            findings.extend(self._guard.scan(text).findings)
        if any(item.severity.value == "critical" for item in findings):
            verdict = SkillScanVerdict.DANGEROUS
        elif findings:
            verdict = SkillScanVerdict.CAUTION
        else:
            verdict = SkillScanVerdict.SAFE
        return SkillScanResult(verdict=verdict, findings=tuple(findings)), scanned

    def _resolve_install_race(
        self,
        preview: SkillInstallPreview,
        target: Path,
    ) -> SkillInstallResult:
        if target.is_symlink() or not target.is_dir():
            raise SkillInstallError(
                SkillInstallErrorCode.CONFLICT,
                f"managed skill target changed during install: {preview.manifest.id}",
            )
        installed_digest = _tree_digest(self._snapshot_tree(target))
        if installed_digest != preview.source_digest:
            raise SkillInstallError(
                SkillInstallErrorCode.CONFLICT,
                f"different skill content won installation race: {preview.manifest.id}",
            )
        return self._finish(SkillInstallStatus.ALREADY_INSTALLED, preview)

    def _finish(
        self,
        status: SkillInstallStatus,
        preview: SkillInstallPreview,
    ) -> SkillInstallResult:
        warnings: list[str] = []
        refreshed = True
        try:
            self._loader.load()
        except (OSError, UnicodeError, ValueError, SkillLoadError) as exc:
            refreshed = False
            warnings.append(
                "managed skill inventory refresh failed: "
                f"{type(exc).__name__}"
            )
        return SkillInstallResult(
            status=status,
            preview=preview,
            inventory_refreshed=refreshed,
            warnings=tuple(warnings),
        )

    def _reject_symlink_chain(self, relative_path: str) -> None:
        current = self._source_root
        for part in Path(relative_path).parts:
            current = current / part
            if current.is_symlink():
                raise SkillInstallError(
                    SkillInstallErrorCode.UNSAFE_PATH,
                    f"skill source path contains symlink: {relative_path}",
                )


def _tree_digest(snapshots: tuple[_FileSnapshot, ...]) -> str:
    payload = [
        {
            "path": item.relative_path,
            "sha256": item.sha256,
            "size": item.size,
        }
        for item in snapshots
    ]
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False
