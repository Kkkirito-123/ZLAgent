"""Bounded GitHub source adapter for standard Agent Skills packages."""

from __future__ import annotations

import io
import json
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib import error, request
from urllib.parse import quote, urlparse

from .installer import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    LocalSkillInstaller,
    SkillInstallError,
    SkillInstallErrorCode,
    SkillInstallStatus,
    validate_skill_id,
)
from .loader import FileSystemSkillLoader
from .lock import SkillLockEntry, SkillLockfile, installed_tree_digest
from .types import SkillFormat, SkillManifest


DEFAULT_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class GitHubSkillSource:
    """Normalized GitHub repository, revision, and package subdirectory."""

    owner: str
    repo: str
    ref: str = "HEAD"
    subpath: str = ""

    @property
    def repository(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    @property
    def canonical(self) -> str:
        suffix = f"#{self.subpath}" if self.subpath else ""
        return f"github:{self.owner}/{self.repo}@{self.ref}{suffix}"


class GitHubArchiveClient(Protocol):
    """Small injectable network boundary used by the remote installer."""

    def resolve_commit(self, source: GitHubSkillSource) -> str:
        """Resolve the requested ref to a full immutable commit SHA."""

    def download_archive(
        self,
        source: GitHubSkillSource,
        commit: str,
    ) -> bytes:
        """Download a bounded repository ZIP for the resolved commit."""


class GitHubApiClient:
    """GitHub-only HTTP client with bounded responses and optional auth."""

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout_seconds: float = 20.0,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> None:
        if timeout_seconds <= 0 or max_archive_bytes <= 0:
            raise ValueError("GitHub client limits must be positive")
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._max_archive_bytes = max_archive_bytes

    def resolve_commit(self, source: GitHubSkillSource) -> str:
        endpoint = (
            "https://api.github.com/repos/"
            f"{source.owner}/{source.repo}/commits/{quote(source.ref, safe='')}"
        )
        data = self._read(endpoint, max_bytes=1024 * 1024)
        try:
            sha = str(json.loads(data)["sha"]).casefold()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.IO_ERROR,
                "GitHub returned an invalid commit response",
            ) from exc
        if not _COMMIT_RE.fullmatch(sha):
            raise SkillInstallError(
                SkillInstallErrorCode.IO_ERROR,
                "GitHub returned an invalid commit SHA",
            )
        return sha

    def download_archive(
        self,
        source: GitHubSkillSource,
        commit: str,
    ) -> bytes:
        if not _COMMIT_RE.fullmatch(commit):
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                "resolved GitHub commit must be a full SHA",
            )
        endpoint = (
            f"https://codeload.github.com/{source.owner}/{source.repo}/zip/{commit}"
        )
        return self._read(endpoint, max_bytes=self._max_archive_bytes)

    def _read(self, url: str, *, max_bytes: int) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "OpenZLAgent-SkillInstaller/1",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        http_request = request.Request(url, headers=headers)
        try:
            with request.urlopen(  # noqa: S310 - host is constructed, not user supplied
                http_request,
                timeout=self._timeout_seconds,
            ) as response:
                data = response.read(max_bytes + 1)
        except error.HTTPError as exc:
            code = (
                SkillInstallErrorCode.SOURCE_NOT_FOUND
                if exc.code == 404
                else SkillInstallErrorCode.IO_ERROR
            )
            raise SkillInstallError(
                code,
                f"GitHub request failed with HTTP {exc.code}",
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise SkillInstallError(
                SkillInstallErrorCode.IO_ERROR,
                f"GitHub request failed: {type(exc).__name__}",
            ) from exc
        if len(data) > max_bytes:
            raise SkillInstallError(
                SkillInstallErrorCode.LIMIT_EXCEEDED,
                "GitHub response exceeds the configured byte limit",
            )
        return data


@dataclass(frozen=True, slots=True)
class GitHubSkillInstallResult:
    """Remote source provenance combined with local install truth."""

    status: SkillInstallStatus
    manifest: SkillManifest
    source: GitHubSkillSource
    resolved_commit: str
    digest: str
    file_count: int
    total_bytes: int
    scan_verdict: str
    finding_ids: tuple[str, ...] = field(default_factory=tuple)
    inventory_refreshed: bool = True
    lock_ref: str = "skills.lock.json"
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_ids", tuple(self.finding_ids))
        object.__setattr__(self, "warnings", tuple(self.warnings))


class GitHubSkillInstaller:
    """Install one GitHub-hosted Agent Skill without executing package code."""

    def __init__(
        self,
        managed_root: Path | str,
        *,
        loader: FileSystemSkillLoader | None = None,
        client: GitHubArchiveClient | None = None,
        lockfile: SkillLockfile | None = None,
        max_files: int = DEFAULT_MAX_FILES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> None:
        root_input = Path(managed_root).expanduser()
        if root_input.is_symlink():
            raise SkillInstallError(
                SkillInstallErrorCode.UNSAFE_PATH,
                "managed Skill root must not be a symlink",
            )
        self._managed_root = root_input.resolve()
        if not self._managed_root.is_dir():
            raise SkillInstallError(
                SkillInstallErrorCode.SOURCE_NOT_FOUND,
                "managed Skill root must be an existing directory",
            )
        if min(
            max_files,
            max_file_bytes,
            max_total_bytes,
            max_depth,
            max_archive_bytes,
        ) <= 0:
            raise ValueError("GitHub Skill installation limits must be positive")
        self._loader = loader or FileSystemSkillLoader(self._managed_root)
        if self._loader.root != self._managed_root:
            raise ValueError("skill loader root must match managed_root")
        self._client = client or GitHubApiClient()
        self._lockfile = lockfile or SkillLockfile(self._managed_root)
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes
        self._max_depth = max_depth
        self._max_archive_bytes = max_archive_bytes

    def validate_source_reference(self, value: object) -> GitHubSkillSource:
        return parse_github_skill_source(value)

    def install(
        self,
        source: object,
        skill_id: object,
    ) -> GitHubSkillInstallResult:
        expected_id = validate_skill_id(skill_id)
        parsed = parse_github_skill_source(source)
        replay = self._locked_replay(parsed, expected_id)
        if replay is not None:
            return replay

        commit = self._client.resolve_commit(parsed)
        if not _COMMIT_RE.fullmatch(commit):
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                "GitHub client returned a non-immutable revision",
            )
        archive = self._client.download_archive(parsed, commit)
        if len(archive) > self._max_archive_bytes:
            raise SkillInstallError(
                SkillInstallErrorCode.LIMIT_EXCEEDED,
                "GitHub Skill archive exceeds the configured byte limit",
            )
        with tempfile.TemporaryDirectory(prefix="zlagent-github-skill-") as tmp:
            imports = Path(tmp) / "imports"
            source_root = imports / expected_id
            source_root.mkdir(parents=True)
            _extract_skill_archive(
                archive,
                parsed,
                source_root,
                max_files=self._max_files,
                max_file_bytes=self._max_file_bytes,
                max_total_bytes=self._max_total_bytes,
                max_depth=self._max_depth,
            )
            installer = LocalSkillInstaller(
                imports,
                self._managed_root,
                loader=self._loader,
                max_files=self._max_files,
                max_file_bytes=self._max_file_bytes,
                max_total_bytes=self._max_total_bytes,
                max_depth=self._max_depth,
            )
            preview = installer.preview(expected_id, expected_id)
            _require_agent_skills_manifest(preview.manifest)
            installed = installer.install(expected_id, expected_id)

        local_preview = installed.preview
        entry = SkillLockEntry(
            skill_id=expected_id,
            name=local_preview.manifest.name,
            version=local_preview.manifest.version,
            source=parsed.canonical,
            repository=parsed.repository,
            requested_ref=parsed.ref,
            resolved_commit=commit,
            subpath=parsed.subpath,
            digest=local_preview.source_digest,
            file_count=local_preview.file_count,
            total_bytes=local_preview.total_bytes,
            scan_verdict=local_preview.scan_result.verdict.value,
            finding_ids=tuple(
                item.pattern_id for item in local_preview.scan_result.findings
            ),
        )
        self._lockfile.record(entry)
        return GitHubSkillInstallResult(
            status=installed.status,
            manifest=local_preview.manifest,
            source=parsed,
            resolved_commit=commit,
            digest=local_preview.source_digest,
            file_count=local_preview.file_count,
            total_bytes=local_preview.total_bytes,
            scan_verdict=local_preview.scan_result.verdict.value,
            finding_ids=entry.finding_ids,
            inventory_refreshed=installed.inventory_refreshed,
            lock_ref=self._lockfile.path.name,
            warnings=installed.warnings,
        )

    def _locked_replay(
        self,
        source: GitHubSkillSource,
        skill_id: str,
    ) -> GitHubSkillInstallResult | None:
        entry = self._lockfile.get(skill_id)
        if entry is None:
            return None
        if entry.source != source.canonical:
            raise SkillInstallError(
                SkillInstallErrorCode.CONFLICT,
                "installed Skill is locked to a different GitHub source",
                details={
                    "skill_id": skill_id,
                    "locked_source": entry.source,
                    "requested_source": source.canonical,
                },
            )
        target = self._managed_root / skill_id
        digest = installed_tree_digest(
            target,
            max_files=self._max_files,
            max_file_bytes=self._max_file_bytes,
            max_total_bytes=self._max_total_bytes,
            max_depth=self._max_depth,
        )
        if digest != entry.digest:
            raise SkillInstallError(
                SkillInstallErrorCode.CONFLICT,
                "installed Skill content no longer matches its lock entry",
                details={
                    "skill_id": skill_id,
                    "locked_digest": entry.digest,
                    "installed_digest": digest,
                },
            )
        self._loader.load()
        manifest = self._loader.get(skill_id)
        if manifest is None:
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                "locked Skill is missing from the managed inventory",
            )
        return GitHubSkillInstallResult(
            status=SkillInstallStatus.ALREADY_INSTALLED,
            manifest=manifest,
            source=source,
            resolved_commit=entry.resolved_commit,
            digest=entry.digest,
            file_count=entry.file_count,
            total_bytes=entry.total_bytes,
            scan_verdict=entry.scan_verdict,
            finding_ids=entry.finding_ids,
            lock_ref=self._lockfile.path.name,
        )


def parse_github_skill_source(value: object) -> GitHubSkillSource:
    """Parse a GitHub URL or ``owner/repo@ref#subpath`` shorthand."""

    raw = str(value or "").strip()
    if not raw:
        raise _invalid_source("GitHub Skill source must be non-empty")
    if raw.startswith("https://"):
        parsed_url = urlparse(raw)
        if parsed_url.hostname != "github.com" or parsed_url.query:
            raise _invalid_source("only https://github.com Skill sources are allowed")
        parts = [part for part in parsed_url.path.split("/") if part]
        if len(parts) < 2:
            raise _invalid_source("GitHub URL must include owner and repository")
        owner, repo = parts[:2]
        repo = repo.removesuffix(".git")
        ref = "HEAD"
        subpath = parsed_url.fragment
        if len(parts) > 2:
            if len(parts) < 4 or parts[2] != "tree":
                raise _invalid_source(
                    "GitHub URL must target a repository or /tree/<ref>/<path>"
                )
            ref = parts[3]
            url_subpath = "/".join(parts[4:])
            if subpath and url_subpath:
                raise _invalid_source("Skill subpath is specified twice")
            subpath = subpath or url_subpath
    else:
        repository_part, separator, subpath = raw.partition("#")
        if separator and "#" in subpath:
            raise _invalid_source("GitHub Skill source has multiple fragments")
        repository_part, at, ref = repository_part.rpartition("@")
        if not at:
            repository_part, ref = raw.partition("#")[0], "HEAD"
        parts = repository_part.split("/")
        if len(parts) != 2:
            raise _invalid_source("GitHub shorthand must be owner/repo[@ref][#path]")
        owner, repo = parts
        repo = repo.removesuffix(".git")
    _validate_owner_repo(owner, "owner")
    _validate_owner_repo(repo, "repository")
    normalized_ref = str(ref or "HEAD").strip()
    if (
        not _REF_RE.fullmatch(normalized_ref)
        or ".." in normalized_ref
        or "//" in normalized_ref
    ):
        raise _invalid_source("GitHub ref contains unsupported characters")
    normalized_subpath = _normalize_subpath(subpath)
    return GitHubSkillSource(
        owner=owner,
        repo=repo,
        ref=normalized_ref,
        subpath=normalized_subpath,
    )


def _extract_skill_archive(
    archive: bytes,
    source: GitHubSkillSource,
    destination: Path,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
    max_depth: int,
) -> None:
    try:
        zipped = zipfile.ZipFile(io.BytesIO(archive))
    except (OSError, zipfile.BadZipFile) as exc:
        raise SkillInstallError(
            SkillInstallErrorCode.INVALID_PACKAGE,
            "GitHub Skill archive is not a valid ZIP",
        ) from exc
    selected: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    selected_paths: set[str] = set()
    total_bytes = 0
    roots: set[str] = set()
    try:
        for info in zipped.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                raise _archive_error(f"unsafe archive path: {info.filename}")
            if not path.parts:
                continue
            roots.add(path.parts[0])
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise _archive_error(f"archive contains symlink: {info.filename}")
            if info.flag_bits & 0x1:
                raise _archive_error(f"archive contains encrypted file: {info.filename}")
            if info.is_dir():
                continue
            if mode not in {0, stat.S_IFREG}:
                raise _archive_error(
                    f"archive contains non-regular file: {info.filename}"
                )
            repository_path = PurePosixPath(*path.parts[1:])
            relative = _relative_to_subpath(repository_path, source.subpath)
            if relative is None:
                continue
            if not relative.parts or len(relative.parts) > max_depth:
                raise SkillInstallError(
                    SkillInstallErrorCode.LIMIT_EXCEEDED,
                    f"Skill archive exceeds depth limit: {relative.as_posix()}",
                )
            normalized_relative = relative.as_posix().casefold()
            if normalized_relative in selected_paths:
                raise _archive_error(
                    f"archive contains duplicate path: {relative.as_posix()}"
                )
            selected_paths.add(normalized_relative)
            if info.file_size > max_file_bytes:
                raise SkillInstallError(
                    SkillInstallErrorCode.LIMIT_EXCEEDED,
                    f"Skill archive file exceeds size limit: {relative.as_posix()}",
                )
            total_bytes += info.file_size
            if total_bytes > max_total_bytes:
                raise SkillInstallError(
                    SkillInstallErrorCode.LIMIT_EXCEEDED,
                    "Skill archive exceeds total byte limit",
                )
            selected.append((info, relative))
            if len(selected) > max_files:
                raise SkillInstallError(
                    SkillInstallErrorCode.LIMIT_EXCEEDED,
                    "Skill archive exceeds file count limit",
                )
        if len(roots) != 1:
            raise _archive_error("GitHub archive must have one repository root")
        for info, relative in selected:
            with zipped.open(info) as handle:
                data = handle.read(max_file_bytes + 1)
                overflow = handle.read(1)
            if (
                overflow
                or len(data) != info.file_size
                or len(data) > max_file_bytes
            ):
                raise _archive_error(
                    f"archive file size changed while reading: {relative.as_posix()}"
                )
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except SkillInstallError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SkillInstallError(
            SkillInstallErrorCode.INVALID_PACKAGE,
            f"GitHub Skill archive extraction failed: {type(exc).__name__}",
        ) from exc
    finally:
        zipped.close()
    if not (destination / "SKILL.md").is_file():
        raise SkillInstallError(
            SkillInstallErrorCode.INVALID_PACKAGE,
            "selected GitHub subpath does not contain a root SKILL.md",
        )


def _require_agent_skills_manifest(manifest: SkillManifest) -> None:
    if manifest.format is not SkillFormat.AGENT_SKILLS:
        raise SkillInstallError(
            SkillInstallErrorCode.INVALID_PACKAGE,
            "GitHub installation requires an Agent Skills SKILL.md package",
        )
    for key in ("name", "description"):
        value = manifest.metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SkillInstallError(
                SkillInstallErrorCode.INVALID_PACKAGE,
                f"Agent Skills frontmatter requires non-empty {key}",
            )


def _relative_to_subpath(
    repository_path: PurePosixPath,
    subpath: str,
) -> PurePosixPath | None:
    if not subpath:
        return repository_path
    try:
        return repository_path.relative_to(PurePosixPath(subpath))
    except ValueError:
        return None


def _normalize_subpath(value: object) -> str:
    text = str(value or "").strip().strip("/")
    if not text:
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\\" in text:
        raise _invalid_source("GitHub Skill subpath must stay inside the repository")
    return path.as_posix()


def _validate_owner_repo(value: str, label: str) -> None:
    if not _OWNER_REPO_RE.fullmatch(value) or value in {".", ".."}:
        raise _invalid_source(f"invalid GitHub {label}")


def _invalid_source(message: str) -> SkillInstallError:
    return SkillInstallError(SkillInstallErrorCode.INVALID_PACKAGE, message)


def _archive_error(message: str) -> SkillInstallError:
    return SkillInstallError(SkillInstallErrorCode.UNSAFE_PATH, message)
