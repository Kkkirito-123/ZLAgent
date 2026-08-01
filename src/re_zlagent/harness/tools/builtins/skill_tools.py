"""Built-in tool for controlled local skill installation."""

from __future__ import annotations

from typing import Any

from ...skills import (
    GitHubSkillInstaller,
    GitHubSkillInstallResult,
    LocalSkillInstaller,
    SkillInstallError,
    SkillInstallErrorCode,
    SkillInstallResult,
    SkillInstallStatus,
    validate_skill_id,
)
from ..base import Tool, ToolPermission, ToolResult
from ..metadata import Evidence, RecommendedNextAction, SideEffect, ToolErrorType


class InstallSkillTool(Tool):
    """Install a validated package from a configured local import root."""

    name = "install_skill"
    description = (
        "Install one local Skill package from the configured import root into "
        "the managed Skill directory. Requires explicit user confirmation, "
        "blocks path escapes and dangerous text, and never overwrites a "
        "different installed package."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = False
    side_effects = ("filesystem",)
    outbox_required = True
    side_effect_retry_safe = True
    max_result_chars = 2_000
    input_schema = {
        "type": "object",
        "properties": {
            "source_path": {
                "type": "string",
                "description": "Package directory relative to the Skill import root.",
            },
            "skill_id": {
                "type": "string",
                "description": "Expected lowercase id from the package manifest.",
            },
        },
        "required": ["source_path", "skill_id"],
    }

    def __init__(self, installer: LocalSkillInstaller) -> None:
        self._installer = installer

    def activity_description(self, arguments: dict[str, Any]) -> str:
        return f"Install Skill {arguments.get('skill_id', '')}".strip()

    def plan_side_effects(
        self,
        arguments: dict[str, Any],
    ) -> tuple[SideEffect, ...]:
        skill_id = validate_skill_id(arguments.get("skill_id"))
        self._installer.validate_source_reference(arguments.get("source_path"))
        return (self._side_effect(skill_id),)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            outcome = self._installer.install(
                arguments.get("source_path"),
                arguments.get("skill_id"),
            )
        except SkillInstallError as exc:
            return _install_error_result(exc, source=self.name)
        return self._success_result(outcome)

    def _success_result(self, outcome: SkillInstallResult) -> ToolResult:
        preview = outcome.preview
        replay = outcome.status is SkillInstallStatus.ALREADY_INSTALLED
        verb = "Already installed" if replay else "Installed"
        metadata = {
            "skill_id": preview.manifest.id,
            "name": preview.manifest.name,
            "version": preview.manifest.version,
            "format": preview.manifest.format.value,
            "source_path": preview.source_path,
            "target": preview.target_ref,
            "digest": preview.source_digest,
            "file_count": preview.file_count,
            "total_bytes": preview.total_bytes,
            "scan_verdict": preview.scan_result.verdict.value,
            "finding_ids": [
                item.pattern_id for item in preview.scan_result.findings
            ],
            "scanned_text_files": preview.scanned_text_files,
            "idempotent_replay": replay,
            "inventory_refreshed": outcome.inventory_refreshed,
            "warnings": list(outcome.warnings),
        }
        return ToolResult.success(
            f"{verb} Skill {preview.manifest.id} ({preview.manifest.version})",
            raw={"status": outcome.status.value, **metadata},
            evidence=[
                Evidence(
                    type="skill_installation",
                    ref=f"skill:{preview.manifest.id}",
                    summary="controlled local Skill package installed",
                    metadata=metadata,
                )
            ],
            side_effects=[self._side_effect(preview.manifest.id)],
            source=self.name,
        )

    @staticmethod
    def _side_effect(skill_id: str) -> SideEffect:
        return SideEffect(
            type="filesystem",
            target=f"skills/{skill_id}",
            risk="low",
            metadata={"skill_id": skill_id},
        )


class InstallGitHubSkillTool(Tool):
    """Install an Agent Skill from an explicitly enabled GitHub source."""

    name = "install_github_skill"
    description = (
        "Install one standard Agent Skill from GitHub into the managed Skill "
        "directory. Resolves the requested ref to an immutable commit, writes "
        "an auditable lock entry, requires confirmation, and never executes "
        "package scripts."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = False
    side_effects = ("filesystem",)
    outbox_required = True
    side_effect_retry_safe = True
    max_result_chars = 2_500
    input_schema = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": (
                    "GitHub URL or owner/repo@ref#path source. Only github.com "
                    "is accepted."
                ),
            },
            "skill_id": {
                "type": "string",
                "description": "Expected lowercase id for the installed Skill.",
            },
        },
        "required": ["source", "skill_id"],
    }

    def __init__(self, installer: GitHubSkillInstaller) -> None:
        self._installer = installer

    def activity_description(self, arguments: dict[str, Any]) -> str:
        return f"Install GitHub Skill {arguments.get('skill_id', '')}".strip()

    def plan_side_effects(
        self,
        arguments: dict[str, Any],
    ) -> tuple[SideEffect, ...]:
        skill_id = validate_skill_id(arguments.get("skill_id"))
        source = self._installer.validate_source_reference(arguments.get("source"))
        return (self._side_effect(skill_id, source.canonical),)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            outcome = self._installer.install(
                arguments.get("source"),
                arguments.get("skill_id"),
            )
        except SkillInstallError as exc:
            return _install_error_result(exc, source=self.name)
        return self._success_result(outcome)

    def _success_result(self, outcome: GitHubSkillInstallResult) -> ToolResult:
        replay = outcome.status is SkillInstallStatus.ALREADY_INSTALLED
        verb = "Already installed" if replay else "Installed"
        metadata = {
            "skill_id": outcome.manifest.id,
            "name": outcome.manifest.name,
            "version": outcome.manifest.version,
            "format": outcome.manifest.format.value,
            "source": outcome.source.canonical,
            "repository": outcome.source.repository,
            "requested_ref": outcome.source.ref,
            "resolved_commit": outcome.resolved_commit,
            "subpath": outcome.source.subpath,
            "target": f"skills/{outcome.manifest.id}",
            "digest": outcome.digest,
            "file_count": outcome.file_count,
            "total_bytes": outcome.total_bytes,
            "scan_verdict": outcome.scan_verdict,
            "finding_ids": list(outcome.finding_ids),
            "idempotent_replay": replay,
            "inventory_refreshed": outcome.inventory_refreshed,
            "lock_ref": outcome.lock_ref,
            "warnings": list(outcome.warnings),
        }
        return ToolResult.success(
            f"{verb} GitHub Skill {outcome.manifest.id} "
            f"at {outcome.resolved_commit[:12]}",
            raw={"status": outcome.status.value, **metadata},
            evidence=[
                Evidence(
                    type="skill_installation",
                    ref=f"skill:{outcome.manifest.id}",
                    summary="pinned GitHub Agent Skill installed",
                    metadata=metadata,
                )
            ],
            side_effects=[
                self._side_effect(outcome.manifest.id, outcome.source.canonical)
            ],
            source=self.name,
        )

    @staticmethod
    def _side_effect(skill_id: str, source: str) -> SideEffect:
        return SideEffect(
            type="filesystem",
            target=f"skills/{skill_id}",
            risk="medium",
            metadata={"skill_id": skill_id, "source": source},
        )


def _install_error_result(
    error: SkillInstallError,
    *,
    source: str,
) -> ToolResult:
    if error.code is SkillInstallErrorCode.CONFLICT:
        error_type = ToolErrorType.CONFLICT
        action = RecommendedNextAction.MANUAL_REVIEW
        recoverable = False
    elif error.code in {
        SkillInstallErrorCode.UNSAFE_PATH,
        SkillInstallErrorCode.LIMIT_EXCEEDED,
        SkillInstallErrorCode.DANGEROUS_CONTENT,
    }:
        error_type = ToolErrorType.UNSAFE_WRITE
        action = RecommendedNextAction.STOP
        recoverable = False
    elif error.code is SkillInstallErrorCode.IO_ERROR:
        error_type = ToolErrorType.EXTERNAL_UNAVAILABLE
        action = RecommendedNextAction.MANUAL_REVIEW
        recoverable = False
    elif error.code is SkillInstallErrorCode.SOURCE_NOT_FOUND:
        error_type = ToolErrorType.NOT_FOUND
        action = RecommendedNextAction.RETRY
        recoverable = True
    else:
        error_type = ToolErrorType.INVALID_INPUT
        action = RecommendedNextAction.RETRY
        recoverable = True
    return ToolResult.failure(
        error.message,
        error_type=error_type,
        recoverable_by_model=recoverable,
        recommended_next_action=action,
        raw={"install_error": error.code.value, **error.details},
        source=source,
    )
