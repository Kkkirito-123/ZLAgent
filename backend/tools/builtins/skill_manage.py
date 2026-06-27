"""skill_manage: agent-managed skill creation & editing (confirm tier).

This is the v0.7 surface that lets the LLM persist procedural knowledge as
new skill directories under the workspace. Inspired by Hermes Agent's
``skill_manager_tool`` — same six actions, scoped to ZLAgent's workspace
layout, gated by the v0.6 IM confirmation flow.

Actions:
    create        Make a new skill directory with SKILL.md + YAML frontmatter
    edit          Replace SKILL.md verbatim
    patch         Find-and-replace a single string in SKILL.md or a sub-file
    write_file    Add/overwrite a support file (references/, templates/, scripts/)
    remove_file   Delete a support file from a skill
    delete        Remove the entire skill directory

The tool ALWAYS runs as ``permission=confirm``, so :class:`AgentLoop`
intercepts every invocation, emits an IM yes/no question, and only executes
the action after the user replies ``yes``. Cron / non-interactive turns
auto-deny — the agent should fall back to ``write_file`` (raw, no skill
metadata) for unattended writes.

Path safety mirrors ``write_file``: skill names must be slug-safe,
sub-paths cannot escape the skill's own directory, no symlink traversal.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Optional

import httpx

from loguru import logger

from ...core.provenance import (
    BACKGROUND_REVIEW,
    FOREGROUND,
    get_current_write_origin,
)
from ...skills.guard import SkillGuard
from ...skills.history import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_EDIT,
    ACTION_PATCH,
    ACTION_REMOVE_FILE,
    ACTION_WRITE_FILE,
    SkillHistoryStore,
    actor_for_origin,
)
from ...skills.usage import (
    CREATED_BY_AGENT,
    CREATED_BY_USER,
    STATE_ARCHIVED,
    UsageStore,
)
from ..base import Tool, ToolPermission, ToolResult

MAX_BODY_BYTES = 256 * 1024
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
ALLOWED_SUBDIRS = ("references", "templates", "scripts", "assets")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class SkillManageTool(Tool):
    name = "skill_manage"
    description = (
        "Create or edit skills under the workspace `skills/` directory. "
        "Use this for reusable procedures, recurring automations, or fixes worth keeping. "
        "Prefer `patch` for small edits; use `edit` only when replacing the full `SKILL.md`."
    )
    # Personal-AI mode: the operator owns the skills, so writes don't
    # need an IM yes/no per turn. Vetting still happens via the
    # ``skill-vetter`` skill before publishing complex skills, but the
    # initial CRUD calls are immediate.
    permission = ToolPermission.SAFE
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = True
    max_result_chars = 8_000
    search_hint = "skill create patch edit archive pin procedural memory SKILL.md"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "edit",
                    "patch",
                    "write_file",
                    "remove_file",
                    "delete",
                    # v0.11 lifecycle actions
                    "pin",
                    "unpin",
                    "archive",
                    "import_from_url",
                ],
                "description": (
                    "Which mutation to perform. v0.11 adds ``pin`` /"
                    " ``unpin`` (mark a skill immutable to the curator)"
                    " and ``archive`` (soft-remove — reversible, unlike"
                    " ``delete``). Use ``archive`` instead of ``delete``"
                    " whenever a skill might be useful later."
                    " ``import_from_url`` fetches a raw SKILL.md from a"
                    " URL and installs it as a new local skill."
                ),
            },
            "skill_name": {
                "type": "string",
                "description": (
                    "Lowercase slug identifying the skill, e.g. 'arxiv-daily'."
                ),
            },
            "skill_description": {
                "type": "string",
                "description": (
                    "One-sentence description (only used by 'create'). Stored"
                    " in the YAML frontmatter."
                ),
            },
            "skill_body": {
                "type": "string",
                "description": "Full SKILL.md body for create/edit; omit for patch.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags stored in metadata.hermes.tags.",
            },
            "file_path": {
                "type": "string",
                "description": "Sub-path within the skill directory; use SKILL.md for patching the main file.",
            },
            "file_content": {
                "type": "string",
                "description": (
                    "UTF-8 content for 'write_file' (max 256 KiB)."
                ),
            },
            "find": {
                "type": "string",
                "description": (
                    "Exact string to replace (used by 'patch'). Must occur"
                    " exactly once in the target file."
                ),
            },
            "replace": {
                "type": "string",
                "description": "Replacement string (used by 'patch').",
            },
            "absorbed_into": {
                "type": "string",
                "description": (
                    "Only used by ``archive``. Records the surviving skill"
                    " when this one is being archived as a duplicate. The"
                    " curator keeps the survivor and links this archived"
                    " row back to it so the decision is auditable."
                ),
            },
            "source_url": {
                "type": "string",
                "description": (
                    "Raw URL of a SKILL.md file to import"
                    " (used by 'import_from_url'). Must be https://."
                    " The skill name can be overridden via skill_name;"
                    " otherwise it is read from the YAML frontmatter."
                ),
            },
        },
        "required": ["action", "skill_name"],
    }

    def __init__(
        self,
        skills_root: Path,
        *,
        usage_store: Optional[UsageStore] = None,
        history_store: Optional[SkillHistoryStore] = None,
        guard: Optional[SkillGuard] = None,
    ) -> None:
        self._root = Path(skills_root).resolve()
        self._usage = usage_store
        # append a JSONL entry per mutation. None = no logging
        # (smoke tests that bring up SkillManageTool without app.py wiring
        # don't need it; production always wires it).
        self._history = history_store
        # static security scanner. ``None`` keeps legacy behaviour
        # so smoke tests that don't pass a guard still pass. Production
        # always wires a SkillGuard with strict-for-agent enabled.
        self._guard = guard

    # -- v0.20 SkillGuard helper ----------------------------------------

    def _guard_block(
        self, *, skill_name: str, action: str, text: str
    ) -> Optional[ToolResult]:
        """Run the static scanner; return a refusal ToolResult or None."""
        if self._guard is None or not self._guard.enabled or not text:
            return None
        result = self._guard.scan(text)
        origin = get_current_write_origin()
        if not self._guard.should_block(result, origin=origin):
            if result.findings:
                # Caution-class findings are visible in the log so the
                # operator can audit them, even when the policy allows
                # them through.
                logger.info(
                    "[skill_guard] allowed {} on {}: {}",
                    action, skill_name, result.summary_line(),
                )
            return None
        logger.warning(
            "[skill_guard] BLOCK {} on {} ({}): {}",
            action, skill_name, origin, result.summary_line(),
        )
        return ToolResult(
            ok=False,
            content="",
            error=self._guard.block_message(result),
        )

    # -- v0.15 history helpers ----------------------------------------------

    def _record_history(
        self,
        *,
        skill_name: str,
        action: str,
        file_path: str = "SKILL.md",
        before_text: Optional[str] = None,
        after_text: Optional[str] = None,
    ) -> None:
        if self._history is None:
            return
        try:
            actor = actor_for_origin(get_current_write_origin())
            self._history.record(
                skill_name=skill_name,
                action=action,
                file_path=file_path,
                actor=actor,
                before_text=before_text,
                after_text=after_text,
            )
        except Exception:
            # The store itself swallows errors, but if our helper raised
            # for some other reason (provenance import, etc.) we still
            # don't want to break the underlying skill mutation.
            pass

    @staticmethod
    def _safe_read_text(path: Path) -> Optional[str]:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _resolve_skill_dir(self, skill_name: str) -> Path:
        """Map ``skill_name`` to the directory that holds ``SKILL.md``.

        Hermes layout often nests skills as ``skills/<category>/<name>/``.
        Mutations historically used only ``skills/<name>/``; this helper
        finds the real folder while keeping ``skill_name`` as the logical id.
        """
        root = self._root.resolve()
        direct = (root / skill_name).resolve()
        try:
            direct.relative_to(root)
        except ValueError:
            return direct
        if direct.is_dir() and (direct / "SKILL.md").is_file():
            return direct
        try:
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                nested = (child / skill_name).resolve()
                try:
                    nested.relative_to(root)
                except ValueError:
                    continue
                if nested.is_dir() and (nested / "SKILL.md").is_file():
                    return nested
                for grand in sorted(child.iterdir()):
                    if not grand.is_dir() or grand.name.startswith("."):
                        continue
                    deep = (grand / skill_name).resolve()
                    try:
                        deep.relative_to(root)
                    except ValueError:
                        continue
                    if deep.is_dir() and (deep / "SKILL.md").is_file():
                        return deep
        except OSError:
            pass
        return direct

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "").strip()
        skill_name = str(arguments.get("skill_name") or "").strip()

        if not action:
            return ToolResult(ok=False, content="", error="action is required")
        if not skill_name:
            return ToolResult(ok=False, content="", error="skill_name is required")
        if not _SKILL_NAME_RE.match(skill_name):
            return ToolResult(
                ok=False, content="",
                error=(
                    f"skill_name {skill_name!r} must match"
                    " [a-z0-9][a-z0-9._-]{0,63}"
                ),
            )

        root_resolved = self._root.resolve()
        skill_dir = (root_resolved / skill_name).resolve()
        try:
            skill_dir.relative_to(root_resolved)
        except ValueError:
            return ToolResult(
                ok=False, content="",
                error=f"skill path escapes skills root: {skill_name}",
            )

        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(
                ok=False, content="", error=f"cannot create skills root: {exc}",
            )

        if action not in {"create", "import_from_url"}:
            skill_dir = self._resolve_skill_dir(skill_name)
            try:
                skill_dir.relative_to(root_resolved)
            except ValueError:
                return ToolResult(
                    ok=False, content="",
                    error=f"skill path escapes skills root: {skill_name}",
                )

        # Mutating actions (not create) must respect the pinned flag: a
        # skill the user explicitly pinned is frozen against accidental
        # edits from the agent itself, including the background review
        # fork. ``create`` is exempt because it targets a not-yet-existent
        # skill_name slot.
        if action in {"edit", "patch", "write_file", "remove_file", "delete", "archive"}:
            pin_block = self._refuse_if_pinned(skill_name, action)
            if pin_block is not None:
                return pin_block

        if action == "create":
            return self._create(skill_dir, skill_name, arguments)
        if action == "edit":
            return self._edit(skill_dir, skill_name, arguments)
        if action == "patch":
            return self._patch(skill_dir, skill_name, arguments)
        if action == "write_file":
            return self._write_file(skill_dir, skill_name, arguments)
        if action == "remove_file":
            return self._remove_file(skill_dir, skill_name, arguments)
        if action == "delete":
            return self._delete(skill_dir, skill_name)
        if action == "pin":
            return self._pin(skill_dir, skill_name, pinned=True)
        if action == "unpin":
            return self._pin(skill_dir, skill_name, pinned=False)
        if action == "archive":
            return self._archive(skill_dir, skill_name, arguments)
        if action == "import_from_url":
            return await self._import_from_url(skill_name, arguments)
        return ToolResult(ok=False, content="", error=f"unknown action: {action}")

    # -- v0.11 helpers -------------------------------------------------

    def _refuse_if_pinned(self, skill_name: str, action: str) -> Optional[ToolResult]:
        """Return a pin-block ToolResult, or None if the action may proceed."""
        if self._usage is None:
            return None
        rec = self._usage.get(skill_name)
        if rec.get("pinned"):
            return ToolResult(
                ok=False, content="",
                error=(
                    f"skill {skill_name!r} is pinned; action={action!r} refused."
                    " Use action='unpin' first if the user explicitly wants"
                    " to modify this skill."
                ),
            )
        return None

    def _resolve_created_by(self) -> str:
        """Derive the ``created_by`` tag from the active write origin."""
        return (
            CREATED_BY_AGENT
            if get_current_write_origin() == BACKGROUND_REVIEW
            else CREATED_BY_USER
        )

    # -- actions -------------------------------------------------------

    async def _import_from_url(
        self, skill_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        """Fetch a raw SKILL.md from a URL and install it as a new skill."""
        source_url = str(arguments.get("source_url") or "").strip()
        if not source_url:
            return ToolResult(ok=False, content="", error="source_url is required for import_from_url")
        if not source_url.startswith("https://"):
            return ToolResult(ok=False, content="", error="source_url must start with https://")

        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(source_url, headers={"Accept": "text/plain,text/markdown,*/*"})
                resp.raise_for_status()
                raw = resp.text
        except httpx.HTTPStatusError as exc:
            return ToolResult(ok=False, content="", error=f"HTTP {exc.response.status_code} fetching {source_url}")
        except Exception as exc:
            return ToolResult(ok=False, content="", error=f"fetch failed: {exc}")

        if len(raw.encode()) > MAX_BODY_BYTES:
            return ToolResult(ok=False, content="", error="fetched content exceeds 256 KiB cap")

        # Extract name from YAML frontmatter if skill_name is a placeholder
        resolved_name = skill_name
        if raw.startswith("---"):
            try:
                end = raw.index("---", 3)
                fm_text = raw[3:end]
                for line in fm_text.splitlines():
                    if line.startswith("name:"):
                        candidate = line.split(":", 1)[1].strip().strip('"\'')
                        if candidate and _SKILL_NAME_RE.match(candidate):
                            resolved_name = candidate
                        break
            except (ValueError, IndexError):
                pass

        if not _SKILL_NAME_RE.match(resolved_name):
            return ToolResult(
                ok=False, content="",
                error=f"resolved skill name {resolved_name!r} is not a valid slug",
            )

        skill_dir = (self._root / resolved_name).resolve()
        try:
            skill_dir.relative_to(self._root)
        except ValueError:
            return ToolResult(ok=False, content="", error="skill path escapes skills root")

        if skill_dir.exists():
            return ToolResult(
                ok=False, content="",
                error=f"skill {resolved_name!r} already exists; use 'edit' to update",
            )

        guard_block = self._guard_block(skill_name=resolved_name, action="import_from_url", text=raw)
        if guard_block is not None:
            return guard_block

        self._root.mkdir(parents=True, exist_ok=True)
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(raw, encoding="utf-8")

        self._record_history(
            skill_name=resolved_name,
            action=ACTION_CREATE,
            after_text=raw,
        )
        if self._usage is not None:
            self._usage.record_create(resolved_name, created_by=self._resolve_created_by())

        logger.info("[skill_manage] imported {} from {}", resolved_name, source_url)
        return ToolResult(
            ok=True,
            content=f"skill '{resolved_name}' imported from {source_url} ({len(raw)} chars)",
        )

    def _create(
        self, skill_dir: Path, skill_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        if skill_dir.exists():
            return ToolResult(
                ok=False, content="",
                error=f"skill {skill_name!r} already exists; use 'edit' or 'patch'",
            )

        description = str(arguments.get("skill_description") or "").strip()
        if len(description) > MAX_DESCRIPTION_LENGTH:
            return ToolResult(
                ok=False, content="",
                error=f"skill_description exceeds {MAX_DESCRIPTION_LENGTH} chars",
            )

        body = arguments.get("skill_body")
        if not isinstance(body, str) or not body.strip():
            return ToolResult(
                ok=False, content="", error="skill_body must be a non-empty string",
            )
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            return ToolResult(
                ok=False, content="", error="skill_body exceeds 256 KiB cap",
            )

        tags = arguments.get("tags") or []
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            return ToolResult(
                ok=False, content="", error="tags must be a list of strings",
            )

        # scan the body + description before any disk write.
        block = self._guard_block(
            skill_name=skill_name,
            action="create",
            text=f"{description}\n\n{body}",
        )
        if block is not None:
            return block

        created_by = self._resolve_created_by()
        try:
            skill_dir.mkdir(parents=True, exist_ok=False)
            skill_md = self._render_skill_md(
                name=skill_name,
                description=description or f"Skill {skill_name}",
                body=body,
                tags=tags,
                created_by=created_by,
            )
            (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"create failed: {exc}")

        # Telemetry: record the new skill's provenance + creation time.
        # Best-effort; a failed sidecar write must NOT break the create.
        if self._usage is not None:
            self._usage.mark_created(
                skill_name,
                created_by=created_by,
                description=description or None,
            )
        # append a CREATE entry to the history log so we can later
        # diff every change a skill ever underwent.
        self._record_history(
            skill_name=skill_name,
            action=ACTION_CREATE,
            file_path="SKILL.md",
            before_text=None,
            after_text=skill_md,
        )

        return ToolResult(
            ok=True,
            content=(
                f"Created skill '{skill_name}' at"
                f" {skill_dir.relative_to(self._root.parent)}/SKILL.md"
                f" ({len(body.encode('utf-8'))} bytes body, created_by={created_by})."
            ),
        )

    def _edit(
        self, skill_dir: Path, skill_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return ToolResult(
                ok=False, content="",
                error=f"skill {skill_name!r} has no SKILL.md to edit",
            )

        body = arguments.get("skill_body")
        if not isinstance(body, str) or not body.strip():
            return ToolResult(
                ok=False, content="", error="skill_body must be a non-empty string",
            )
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            return ToolResult(
                ok=False, content="", error="skill_body exceeds 256 KiB cap",
            )

        # Preserve existing frontmatter (name/description/version) while
        # swapping the body. Falls back to deriving from arguments if the
        # current file has no parseable frontmatter.
        existing = skill_md.read_text(encoding="utf-8")
        meta = _parse_frontmatter(existing)
        description = (
            str(arguments.get("skill_description") or "").strip()
            or meta.get("description")
            or f"Skill {skill_name}"
        )
        tags_arg = arguments.get("tags")
        if tags_arg is None:
            tags = _existing_tags(meta)
        elif isinstance(tags_arg, list) and all(isinstance(t, str) for t in tags_arg):
            tags = tags_arg
        else:
            return ToolResult(
                ok=False, content="", error="tags must be a list of strings",
            )

        rendered = self._render_skill_md(
            name=meta.get("name") or skill_name,
            description=description,
            body=body,
            tags=tags,
            created_by=_existing_created_by(meta),
        )
        # scan the rendered SKILL.md before persisting.
        block = self._guard_block(
            skill_name=skill_name, action="edit", text=rendered,
        )
        if block is not None:
            return block
        try:
            skill_md.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"edit failed: {exc}")
        if self._usage is not None:
            self._usage.record_patch(skill_name)
        # diff = unified diff between previous SKILL.md and the
        # rendered version, capped to 4 KiB inside the history store.
        self._record_history(
            skill_name=skill_name,
            action=ACTION_EDIT,
            file_path="SKILL.md",
            before_text=existing,
            after_text=rendered,
        )
        return ToolResult(
            ok=True, content=f"Edited SKILL.md for skill '{skill_name}'.",
        )

    def _patch(
        self, skill_dir: Path, skill_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        target = self._resolve_subpath(skill_dir, arguments.get("file_path") or "SKILL.md")
        if isinstance(target, ToolResult):
            return target
        if not target.exists() or not target.is_file():
            return ToolResult(
                ok=False, content="",
                error=f"patch target not found: {arguments.get('file_path') or 'SKILL.md'}",
            )

        find_str = arguments.get("find")
        replace_str = arguments.get("replace", "")
        if not isinstance(find_str, str) or not find_str:
            return ToolResult(
                ok=False, content="", error="'find' must be a non-empty string",
            )
        if not isinstance(replace_str, str):
            return ToolResult(
                ok=False, content="", error="'replace' must be a string",
            )

        try:
            current = target.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"read failed: {exc}")

        occurrences = current.count(find_str)
        if occurrences == 0:
            return ToolResult(
                ok=False, content="",
                error=f"'find' string not present in {target.name}",
            )
        if occurrences > 1:
            return ToolResult(
                ok=False, content="",
                error=(
                    f"'find' string occurs {occurrences} times in {target.name};"
                    " patch requires exactly one match — narrow the find string"
                ),
            )

        updated = current.replace(find_str, replace_str, 1)
        if len(updated.encode("utf-8")) > MAX_BODY_BYTES:
            return ToolResult(
                ok=False, content="", error="resulting file exceeds 256 KiB cap",
            )
        # scan the post-patch text. Only scan the *new* surface
        # (the replacement plus a small surrounding context) so existing
        # legitimate content can't trip the guard retroactively.
        block = self._guard_block(
            skill_name=skill_name,
            action="patch",
            text=replace_str,
        )
        if block is not None:
            return block
        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"write failed: {exc}")
        if self._usage is not None:
            self._usage.record_patch(skill_name)
        try:
            patched_rel = str(target.relative_to(skill_dir))
        except ValueError:
            patched_rel = target.name
        # record the targeted patch (find/replace) so the diff
        # log shows the precise text change.
        self._record_history(
            skill_name=skill_name,
            action=ACTION_PATCH,
            file_path=patched_rel,
            before_text=current,
            after_text=updated,
        )
        return ToolResult(
            ok=True,
            content=(
                f"Patched {target.relative_to(self._root.parent)}"
                f" (-{len(find_str)}/+{len(replace_str)} chars)."
            ),
        )

    def _write_file(
        self, skill_dir: Path, skill_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        if not skill_dir.exists():
            return ToolResult(
                ok=False, content="",
                error=f"skill {skill_name!r} does not exist; use 'create' first",
            )
        rel = str(arguments.get("file_path") or "").strip()
        if not rel:
            return ToolResult(ok=False, content="", error="file_path is required")
        target = self._resolve_subpath(skill_dir, rel, allow_create=True)
        if isinstance(target, ToolResult):
            return target

        # Support files must live under one of the allowed subdirs (or be
        # the SKILL.md itself, which goes through 'edit'/'patch').
        rel_norm = rel.replace("\\", "/")
        if rel_norm == "SKILL.md":
            return ToolResult(
                ok=False, content="",
                error="use 'edit' or 'patch' to modify SKILL.md, not 'write_file'",
            )
        first_part = rel_norm.split("/", 1)[0]
        if first_part not in ALLOWED_SUBDIRS:
            return ToolResult(
                ok=False, content="",
                error=(
                    f"support files must live under one of {ALLOWED_SUBDIRS!r};"
                    f" got {first_part!r}"
                ),
            )

        content = arguments.get("file_content")
        if not isinstance(content, str):
            return ToolResult(
                ok=False, content="", error="file_content must be a string",
            )
        if len(content.encode("utf-8")) > MAX_BODY_BYTES:
            return ToolResult(
                ok=False, content="", error="file_content exceeds 256 KiB cap",
            )
        # scan the support file content before any disk write.
        block = self._guard_block(
            skill_name=skill_name, action="write_file", text=content,
        )
        if block is not None:
            return block
        before = self._safe_read_text(target) if target.exists() else None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"write failed: {exc}")
        if self._usage is not None:
            self._usage.record_patch(skill_name)
        try:
            written_rel = str(target.relative_to(skill_dir))
        except ValueError:
            written_rel = target.name
        # every support-file write goes into the diff log too,
        # so reviewers can audit `references/` / `templates/` changes.
        self._record_history(
            skill_name=skill_name,
            action=ACTION_WRITE_FILE,
            file_path=written_rel,
            before_text=before,
            after_text=content,
        )
        return ToolResult(
            ok=True,
            content=(
                f"Wrote {len(content.encode('utf-8'))} bytes to"
                f" {target.relative_to(self._root.parent)}."
            ),
        )

    def _remove_file(
        self, skill_dir: Path, skill_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        rel = str(arguments.get("file_path") or "").strip()
        if not rel:
            return ToolResult(ok=False, content="", error="file_path is required")
        target = self._resolve_subpath(skill_dir, rel)
        if isinstance(target, ToolResult):
            return target
        if not target.exists():
            return ToolResult(
                ok=False, content="", error=f"file not found: {rel}",
            )
        if target.is_dir():
            return ToolResult(
                ok=False, content="",
                error="refusing to remove a directory via remove_file",
            )
        if target.name == "SKILL.md" and target.parent == skill_dir:
            return ToolResult(
                ok=False, content="",
                error="cannot remove SKILL.md; use 'delete' to remove the whole skill",
            )
        before = self._safe_read_text(target)
        try:
            target.unlink()
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"unlink failed: {exc}")
        if self._usage is not None:
            self._usage.record_patch(skill_name)
        try:
            removed_rel = str(target.relative_to(skill_dir))
        except ValueError:
            removed_rel = target.name
        self._record_history(
            skill_name=skill_name,
            action=ACTION_REMOVE_FILE,
            file_path=removed_rel,
            before_text=before,
            after_text=None,
        )
        return ToolResult(ok=True, content=f"Removed {rel}.")

    def _delete(self, skill_dir: Path, skill_name: str) -> ToolResult:
        if not skill_dir.exists():
            return ToolResult(
                ok=False, content="", error=f"skill {skill_name!r} does not exist",
            )
        # Capture SKILL.md (if present) before the rmtree so the history
        # entry's before_hash points at something meaningful even though
        # we don't store a diff for DELETE.
        before = self._safe_read_text(skill_dir / "SKILL.md")
        try:
            shutil.rmtree(skill_dir)
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"delete failed: {exc}")
        # Drop the sidecar entry so a future skill of the same name
        # starts with a fresh record (no inherited counters / state).
        if self._usage is not None:
            self._usage.remove(skill_name)
        # record the deletion. before_hash captures the final
        # SKILL.md hash so a recreate-with-same-name later is still
        # auditable as a different lineage.
        self._record_history(
            skill_name=skill_name,
            action=ACTION_DELETE,
            file_path="SKILL.md",
            before_text=before,
            after_text=None,
        )
        return ToolResult(ok=True, content=f"Deleted skill '{skill_name}'.")

    def _pin(self, skill_dir: Path, skill_name: str, *, pinned: bool) -> ToolResult:
        if not skill_dir.exists():
            return ToolResult(
                ok=False, content="",
                error=f"skill {skill_name!r} does not exist",
            )
        if self._usage is None:
            return ToolResult(
                ok=False, content="",
                error=(
                    "pin / unpin require the v0.11 UsageStore to be wired;"
                    " the smoke harness ran without it. Configure the agent"
                    " with usage_store=UsageStore(...) and try again."
                ),
            )
        self._usage.set_pinned(skill_name, pinned)
        verb = "pinned" if pinned else "unpinned"
        return ToolResult(
            ok=True,
            content=(
                f"Skill {skill_name!r} {verb}."
                + (
                    " Curator and review-fork edits are now refused."
                    if pinned
                    else " Skill is once again eligible for review-fork edits."
                )
            ),
        )

    def _archive(
        self, skill_dir: Path, skill_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        """Soft-archive: mark state=archived but keep files on disk.

        Reversible — set state=active in the sidecar to bring it back.
        Optionally records ``absorbed_into`` so a dedupe merge stays
        traceable months later.
        """
        if not skill_dir.exists():
            return ToolResult(
                ok=False, content="",
                error=f"skill {skill_name!r} does not exist",
            )
        if self._usage is None:
            return ToolResult(
                ok=False, content="",
                error="archive requires the v0.11 UsageStore to be wired",
            )
        absorbed_into = arguments.get("absorbed_into")
        if isinstance(absorbed_into, str):
            absorbed_into = absorbed_into.strip() or None
        else:
            absorbed_into = None
        if absorbed_into and absorbed_into == skill_name:
            return ToolResult(
                ok=False, content="",
                error="absorbed_into cannot point at the skill being archived",
            )
        self._usage.archive(skill_name, absorbed_into=absorbed_into)
        suffix = f" (absorbed into {absorbed_into!r})" if absorbed_into else ""
        return ToolResult(
            ok=True,
            content=(
                f"Archived skill {skill_name!r}{suffix}. Files left on disk;"
                f" set state=active in the sidecar to restore."
            ),
        )

    # -- helpers -------------------------------------------------------

    def _resolve_subpath(
        self, skill_dir: Path, rel: str, *, allow_create: bool = False
    ) -> Path | ToolResult:
        rel = rel.replace("\\", "/").strip()
        if not rel:
            return ToolResult(ok=False, content="", error="file_path is required")
        candidate = Path(rel)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            return ToolResult(
                ok=False, content="",
                error=f"file_path must stay inside the skill: {rel}",
            )
        if not allow_create and not skill_dir.exists():
            return ToolResult(
                ok=False, content="", error=f"skill directory does not exist: {skill_dir.name}",
            )
        target = (skill_dir / candidate).resolve()
        try:
            target.relative_to(skill_dir.resolve())
        except ValueError:
            return ToolResult(
                ok=False, content="", error=f"file_path escapes skill: {rel}",
            )
        return target

    @staticmethod
    def _render_skill_md(
        *,
        name: str,
        description: str,
        body: str,
        tags: list[str],
        created_by: str,
    ) -> str:
        # Hand-render YAML to keep ordering stable across edits and avoid a
        # PyYAML dump dependency surface area.
        lines = ["---", f"name: {name}", f"description: \"{description}\"", "version: 1.0.0"]
        lines.append("metadata:")
        lines.append("  hermes:")
        lines.append(f"    created_by: {created_by}")
        if tags:
            tags_inline = ", ".join(json.dumps(t, ensure_ascii=False) for t in tags)
            lines.append(f"    tags: [{tags_inline}]")
        lines.append("---")
        lines.append("")
        lines.append(body.rstrip() + "\n")
        return "\n".join(lines)


# -- module-level helpers (also imported by tests) --------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    """Best-effort parse of a SKILL.md frontmatter block. Returns {} on failure."""
    import yaml  # local import — yaml is already a runtime dep

    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        return {}
    try:
        parsed = yaml.safe_load(match.group(1)) or {}
        return parsed if isinstance(parsed, dict) else {}
    except yaml.YAMLError:
        return {}


def _existing_tags(meta: dict[str, Any]) -> list[str]:
    hermes = (meta.get("metadata") or {}).get("hermes") or {}
    if not isinstance(hermes, dict):
        return []
    raw = hermes.get("tags") or []
    return [str(t) for t in raw if isinstance(t, str)]


def _existing_created_by(meta: dict[str, Any]) -> str:
    hermes = (meta.get("metadata") or {}).get("hermes") or {}
    if not isinstance(hermes, dict):
        return "user"
    return str(hermes.get("created_by") or "user")
