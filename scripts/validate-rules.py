#!/usr/bin/env python3
"""Validate the portable rules package using Python's standard library and Git."""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


SCRIPT_PATH = Path(__file__).absolute()
ROOT = SCRIPT_PATH.parents[1].resolve()
PORTABLE_ROOT_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "LICENSE",
    "ATTRIBUTIONS.md",
    ".gitignore",
)
TEMPLATE_ROOT_FILES = (
    "AGENTS.zh-CN.md",
    "README.md",
    "README.zh-CN.md",
)
TEMPLATE_AUTOMATION_FILES = (".github/workflows/validate.yml",)
REQUIRED_SKILLS = {
    "bootstrap-repository",
    "define-requirement",
    "deliver-change",
    "implement-change",
    "publish-change",
    "sync-project-guide",
}
MIT_LICENSE_TEXT = """MIT License

Copyright (c) 2026 Kkkirito-123

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
TEMPLATE_ATTRIBUTION_SOURCES = (
    "https://developers.openai.com/codex/guides/agents-md",
    "https://developers.openai.com/codex/skills",
    (
        "https://github.com/openai/skills/tree/"
        "49f948faa9258a0c61caceaf225e179651397431/skills/.system/skill-creator"
    ),
    "https://code.claude.com/docs/en/memory",
    "https://agentskills.io/specification",
    "https://google.github.io/eng-practices/review/developer/small-cls.html",
    "https://microsoft.github.io/code-with-engineering-playbook/code-reviews/pull-requests/",
    "https://github.com/Core-Mate/OpenGUI/tree/7cf28b90866459e74300869766896f953761dd60",
    "https://github.com/bytedance/deer-flow/tree/1a1c5def0da35e8347009fe1fed8e0e2321b0ede",
    "https://mp.weixin.qq.com/s/mGGIbFyF4U1PrBJVdfgcvg",
    "https://github.com/obra/superpowers/tree/d884ae04edebef577e82ff7c4e143debd0bbec99",
    "https://github.com/addyosmani/agent-skills/tree/2fbfa004a0192529bc997d103fc12f19a3804aab",
    (
        "https://github.com/multica-ai/andrej-karpathy-skills/tree/"
        "2c606141936f1eeef17fa3043a72095b4765b9c2"
    ),
    "https://github.com/actions/checkout/tree/3d3c42e5aac5ba805825da76410c181273ba90b1",
    "https://github.com/actions/setup-python/tree/5fda3b95a4ea91299a34e894583c3862153e4b97",
)
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*]\(([^)]+)\)")
MARKDOWN_REFERENCE_DEFINITION_RE = re.compile(
    r"(?m)^[ \t]{0,3}\[((?!\^)[^\]\r\n]+)\]:[ \t]*(<[^>\r\n]+>|\S+)"
)
MARKDOWN_REFERENCE_USE_RE = re.compile(
    r"!?\[([^\]\r\n]+)\]\[([^\]\r\n]*)\]"
)
HTML_LINK_RE = re.compile(
    r"(?i)\b(?:href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))"
)
SKILL_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_-])\$([a-z0-9]+(?:-[a-z0-9]+)*)(?![A-Za-z0-9_-])"
)
URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
    ),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}
IGNORED_SURFACE_PARTS = {".git", "__pycache__"}
MAX_RESOURCE_BYTES = 10 * 1024 * 1024
RULE_PATHSPECS = (
    "AGENTS.md",
    ":(glob)**/AGENTS.md",
    "AGENTS.zh-CN.md",
    "CLAUDE.md",
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "ATTRIBUTIONS.md",
    ".gitignore",
    ".github/workflows/validate.yml",
    ".agents/skills",
    "scripts",
)

errors: list[str] = []
checks: list[str] = []
resource_bytes_cache: dict[Path, bytes | None] = {}


def relative(path: Path) -> str:
    try:
        display = path.relative_to(ROOT).as_posix()
    except ValueError:
        display = "<outside-repository>"
    for pattern in SECRET_PATTERNS.values():
        display = pattern.sub("<redacted>", display)
    return json.dumps(display, ensure_ascii=True)


def uses_symlink_component(path: Path) -> bool:
    """Return True when path or an in-repository parent is a symlink."""
    try:
        parts = path.relative_to(ROOT).parts
    except ValueError:
        return True
    current = ROOT
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def fail(message: str) -> None:
    errors.append(message)


def passed(message: str) -> None:
    checks.append(message)


def pass_if_clean(message: str, previous_error_count: int) -> None:
    if len(errors) == previous_error_count:
        passed(message)


def read_resource_bytes(path: Path) -> bytes | None:
    if path in resource_bytes_cache:
        return resource_bytes_cache[path]
    try:
        size = path.stat().st_size
        if size > MAX_RESOURCE_BYTES:
            fail(f"{relative(path)}: rules-package resource exceeds 10 MiB")
            resource_bytes_cache[path] = None
            return None
        data = path.read_bytes()
    except OSError as exc:
        fail(
            f"{relative(path)}: cannot read rules-package resource "
            f"({exc.__class__.__name__}; details suppressed)"
        )
        resource_bytes_cache[path] = None
        return None
    if len(data) > MAX_RESOURCE_BYTES:
        fail(f"{relative(path)}: rules-package resource exceeds 10 MiB")
        resource_bytes_cache[path] = None
        return None
    resource_bytes_cache[path] = data
    return data


def read_text(path: Path) -> str:
    data = read_resource_bytes(path)
    if data is None:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        fail(
            f"{relative(path)}: cannot read as UTF-8 "
            f"({exc.__class__.__name__}; details suppressed)"
        )
        return ""


def read_text_resource(path: Path) -> str | None:
    """Return UTF-8 text, or None for a binary resource, without echoing content."""
    data = read_resource_bytes(path)
    if data is None:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeError:
        return None


def parse_frontmatter(path: Path, text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        fail(f"{relative(path)}: missing opening YAML frontmatter marker")
        return {}
    try:
        closing = lines.index("---", 1)
    except ValueError:
        fail(f"{relative(path)}: missing closing YAML frontmatter marker")
        return {}

    values: dict[str, str] = {}
    for index, line in enumerate(lines[1:closing], start=2):
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            fail(f"{relative(path)}:{index}: invalid frontmatter field")
            continue
        match = re.fullmatch(r"([a-z][a-z0-9_-]*):\s*(.+)", line)
        if not match:
            fail(f"{relative(path)}:{index}: invalid frontmatter field")
            continue
        key, raw_value = match.groups()
        if key in values:
            fail(f"{relative(path)}:{index}: duplicate frontmatter field")
            continue
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            fail(
                f"{relative(path)}:{index}: frontmatter value must be a "
                "JSON-quoted YAML string"
            )
            continue
        if not isinstance(value, str) or not value:
            fail(f"{relative(path)}:{index}: invalid frontmatter string")
            continue
        if raw_value != json.dumps(value, ensure_ascii=False) or any(
            ord(character) < 32 for character in value
        ):
            fail(f"{relative(path)}:{index}: frontmatter string is not canonical")
            continue
        values[key] = value
    return values


def parse_openai_metadata(path: Path, text: str) -> dict[str, dict[str, object]]:
    """Parse the intentionally small agents/openai.yaml schema exactly."""
    result: dict[str, dict[str, object]] = {}
    current_section: str | None = None
    allowed_sections = {"interface", "policy"}
    allowed_fields = {
        "interface": {"display_name", "short_description", "default_prompt"},
        "policy": {"allow_implicit_invocation"},
    }

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if "\t" in line:
            fail(f"{relative(path)}:{line_number}: tabs are not allowed in metadata")
            continue

        if not line.startswith(" "):
            match = re.fullmatch(r"([a-z_]+):", line)
            if not match or match.group(1) not in allowed_sections:
                fail(f"{relative(path)}:{line_number}: invalid metadata section")
                current_section = None
                continue
            section = match.group(1)
            if section in result:
                fail(f"{relative(path)}:{line_number}: duplicate metadata section")
                current_section = None
                continue
            result[section] = {}
            current_section = section
            continue

        match = re.fullmatch(r"  ([a-z_]+):\s*(.+)", line)
        if not match or current_section is None:
            fail(f"{relative(path)}:{line_number}: invalid metadata indentation")
            continue
        key, raw_value = match.groups()
        if key not in allowed_fields[current_section]:
            fail(f"{relative(path)}:{line_number}: unsupported metadata field")
            continue
        if key in result[current_section]:
            fail(f"{relative(path)}:{line_number}: duplicate metadata field")
            continue

        if raw_value in {"true", "false"}:
            value: object = raw_value == "true"
        else:
            try:
                value = json.loads(raw_value)
            except (json.JSONDecodeError, TypeError):
                fail(f"{relative(path)}:{line_number}: invalid metadata value")
                continue
            if not isinstance(value, str):
                fail(f"{relative(path)}:{line_number}: metadata value has wrong type")
                continue
            if raw_value != json.dumps(value, ensure_ascii=False) or any(
                ord(character) < 32 for character in value
            ):
                fail(f"{relative(path)}:{line_number}: metadata string is not canonical")
                continue
        if current_section == "interface" and not isinstance(value, str):
            fail(f"{relative(path)}:{line_number}: interface value must be a string")
            continue
        if current_section == "policy" and not isinstance(value, bool):
            fail(f"{relative(path)}:{line_number}: policy value must be a boolean")
            continue
        result[current_section][key] = value

    return result


def validate_root_files(template_mode: bool) -> list[Path]:
    previous_error_count = len(errors)
    template_files = TEMPLATE_ROOT_FILES + TEMPLATE_AUTOMATION_FILES
    required = PORTABLE_ROOT_FILES + (template_files if template_mode else ())
    files: list[Path] = []
    for name in required:
        path = ROOT / name
        if uses_symlink_component(path):
            fail(f"{relative(path)}: root rules file must not be a symlink")
        elif not path.is_file():
            fail(f"missing required file: {name}")
        else:
            files.append(path)

    if not template_mode:
        for name in template_files:
            path = ROOT / name
            if uses_symlink_component(path):
                fail(f"{relative(path)}: root rules file must not be a symlink")
            elif path.is_file():
                files.append(path)

    validator = ROOT / "scripts/validate-rules.py"
    if uses_symlink_component(validator):
        fail(f"{relative(validator)}: validator must not be a symlink")
    elif not validator.is_file():
        fail("missing required validator: scripts/validate-rules.py")
    else:
        files.append(validator)
    regression_tests = ROOT / "scripts/test_validate_rules.py"
    if uses_symlink_component(regression_tests):
        fail(f"{relative(regression_tests)}: regression tests must not use a symlink")
    elif not regression_tests.is_file():
        fail("missing validator regression tests: scripts/test_validate_rules.py")
    else:
        files.append(regression_tests)
    pass_if_clean(
        "template adoption files" if template_mode else "portable root files",
        previous_error_count,
    )
    return sorted(set(files))


def validate_license_and_attributions(template_mode: bool) -> None:
    previous_error_count = len(errors)
    license_text = read_text(ROOT / "LICENSE")
    attributions = read_text(ROOT / "ATTRIBUTIONS.md")

    if not license_text.strip():
        fail("LICENSE must contain an explicit repository license")
    if not attributions.strip():
        fail("ATTRIBUTIONS.md must describe external-source scope")

    if template_mode:
        if license_text != MIT_LICENSE_TEXT:
            fail("template LICENSE must match the canonical approved MIT text")
        missing_sources = [
            source
            for source in TEMPLATE_ATTRIBUTION_SOURCES
            if source not in attributions
        ]
        if missing_sources:
            fail("ATTRIBUTIONS.md is missing one or more audited source records")
        if (
            "Apart from standard license notices reproduced for their intended "
            "purpose" not in attributions
        ):
            fail("ATTRIBUTIONS.md must state the audited inclusion boundary")

        readme = read_text(ROOT / "README.md")
        readme_zh = read_text(ROOT / "README.zh-CN.md")
        readmes = (
            (ROOT / "README.md", readme),
            (ROOT / "README.zh-CN.md", readme_zh),
        )
        for path, text in readmes:
            if "[MIT License](LICENSE)" not in text:
                fail(f"{relative(path)}: missing local MIT License link")
            if "(ATTRIBUTIONS.md)" not in text:
                fail(f"{relative(path)}: missing local attribution-register link")

        stale_claims = (
            "currently has no root `LICENSE`",
            "本仓库目前没有根 `LICENSE`",
        )
        for path in (
            ROOT / "AGENTS.md",
            ROOT / "AGENTS.zh-CN.md",
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
        ):
            text = read_text(path)
            if any(claim in text for claim in stale_claims):
                fail(f"{relative(path)}: contains a stale no-license claim")

    pass_if_clean("license and source attribution", previous_error_count)


def validate_skills() -> tuple[list[Path], set[str]]:
    previous_error_count = len(errors)
    skills_root = ROOT / ".agents/skills"
    if uses_symlink_component(skills_root):
        fail("skill directory or one of its parents must not be a symlink")
        return [], set()
    if not skills_root.is_dir():
        fail("missing skill directory: .agents/skills")
        return [], set()

    skill_dirs: list[Path] = []
    for path in sorted(skills_root.iterdir()):
        if path.is_symlink():
            fail(f"{relative(path)}: Skill directory must not be a symlink")
        elif path.is_dir():
            skill_dirs.append(path)
    discovered = {path.name for path in skill_dirs}
    if not REQUIRED_SKILLS.issubset(discovered):
        fail("one or more required personal-baseline Skills are missing")

    markdown_files: list[Path] = []
    declared_names: set[str] = set()
    for directory in skill_dirs:
        skill_path = directory / "SKILL.md"
        metadata_path = directory / "agents/openai.yaml"
        if len(directory.name) > 64 or not SKILL_NAME_RE.fullmatch(directory.name):
            fail(f"{relative(directory)}: invalid Skill directory name")
        if uses_symlink_component(skill_path):
            fail(f"{relative(skill_path)}: SKILL.md must not use a symlink")
            continue
        if not skill_path.is_file():
            fail(f"{relative(directory)}: missing SKILL.md")
            continue

        markdown_files.append(skill_path)
        skill_text = read_text(skill_path)
        values = parse_frontmatter(skill_path, skill_text)
        if set(values) - {"name", "description"}:
            fail(f"{relative(skill_path)}: unsupported frontmatter field")

        name = values.get("name", "")
        description = values.get("description", "")
        if name != directory.name:
            fail(f"{relative(skill_path)}: declared name does not match its directory")
        if name in declared_names:
            fail(f"{relative(skill_path)}: duplicate declared Skill name")
        if name:
            declared_names.add(name)
        if not description:
            fail(f"{relative(skill_path)}: missing description")
        elif len(description) > 1024:
            fail(f"{relative(skill_path)}: description exceeds 1024 characters")
        if len(skill_text.splitlines()) > 500:
            fail(f"{relative(skill_path)}: SKILL.md exceeds 500 lines")

        if uses_symlink_component(metadata_path):
            fail(f"{relative(metadata_path)}: metadata must not use a symlink")
            continue
        if not metadata_path.is_file():
            fail(f"{relative(directory)}: missing agents/openai.yaml")
            continue
        metadata = parse_openai_metadata(metadata_path, read_text(metadata_path))
        expected_interface = {
            "display_name",
            "short_description",
            "default_prompt",
        }
        interface = metadata.get("interface", {})
        if set(interface) != expected_interface:
            fail(f"{relative(metadata_path)}: incomplete or unsupported interface fields")
        display_name = interface.get("display_name")
        short_description = interface.get("short_description")
        default_prompt = interface.get("default_prompt")
        if not isinstance(display_name, str) or not display_name:
            fail(f"{relative(metadata_path)}: invalid display_name")
        if not isinstance(short_description, str) or not 25 <= len(short_description) <= 64:
            fail(f"{relative(metadata_path)}: short_description must be 25-64 characters")
        if not isinstance(default_prompt, str) or f"${directory.name}" not in default_prompt:
            fail(f"{relative(metadata_path)}: default_prompt must reference its Skill")

        policy = metadata.get("policy")
        if policy is not None and set(policy) != {"allow_implicit_invocation"}:
            fail(f"{relative(metadata_path)}: incomplete or unsupported policy fields")
        if policy is not None and not isinstance(
            policy.get("allow_implicit_invocation"), bool
        ):
            fail(
                f"{relative(metadata_path)}: allow_implicit_invocation must be "
                "a boolean"
            )
        if directory.name == "publish-change":
            if policy != {"allow_implicit_invocation": False}:
                fail(
                    f"{relative(metadata_path)}: publish-change must explicitly "
                    "disable implicit invocation"
                )

    pass_if_clean(
        f"Skill structure and metadata ({len(skill_dirs)} discovered)",
        previous_error_count,
    )
    return markdown_files, discovered


def skill_references(path: Path, discovered: set[str]) -> set[str]:
    candidates = set(SKILL_REF_RE.findall(read_text(path)))
    return candidates.intersection(discovered)


def validate_route_document(path: Path, discovered: set[str]) -> set[str]:
    refs = skill_references(path, discovered)
    if not discovered.issubset(refs):
        fail(f"{relative(path)}: one or more discovered Skills lack a route")
    return refs


def validate_routing(discovered: set[str], template_mode: bool) -> None:
    previous_error_count = len(errors)
    agents = ROOT / "AGENTS.md"
    agents_refs = validate_route_document(agents, discovered)

    agents_zh = ROOT / "AGENTS.zh-CN.md"
    if agents_zh.is_file():
        zh_refs = validate_route_document(agents_zh, discovered)
        if agents_refs != zh_refs:
            fail("AGENTS.md and AGENTS.zh-CN.md use different Skill reference sets")

    if template_mode:
        readme = ROOT / "README.md"
        readme_zh = ROOT / "README.zh-CN.md"
        readme_refs = validate_route_document(readme, discovered)
        readme_zh_refs = validate_route_document(readme_zh, discovered)
        if readme_refs != readme_zh_refs:
            fail("README.md and README.zh-CN.md use different Skill reference sets")

    pass_if_clean("Skill routes and retained bilingual reference parity", previous_error_count)


def validate_claude_import() -> None:
    previous_error_count = len(errors)
    path = ROOT / "CLAUDE.md"
    text = read_text(path)
    expected = "# Claude Code Instructions\n\n@AGENTS.md\n"
    if text != expected:
        fail("CLAUDE.md must be the exact conflict-free thin @AGENTS.md adapter")
    pass_if_clean("thin Claude Code import", previous_error_count)


def local_link_target(raw_target: str) -> tuple[str | None, bool]:
    target = html.unescape(raw_target).strip()
    bracketed = target.startswith("<") and target.endswith(">")
    if bracketed:
        target = target[1:-1]
    if not bracketed and " " in target:
        target = target.split(" ", 1)[0]
    if not target or target.startswith("#"):
        return None, False
    target = unquote(target.split("#", 1)[0])
    if WINDOWS_DRIVE_RE.match(target):
        return target, True
    if URI_SCHEME_RE.match(target):
        return (target, True) if target.lower().startswith("file:") else (None, False)
    return (target or None), False


def validate_local_links(markdown_files: list[Path]) -> None:
    previous_error_count = len(errors)
    for path in markdown_files:
        text = read_text(path)
        targets: list[tuple[str, int]] = [
            (match.group(1), match.start()) for match in MARKDOWN_LINK_RE.finditer(text)
        ]

        definitions: set[str] = set()
        for match in MARKDOWN_REFERENCE_DEFINITION_RE.finditer(text):
            label = " ".join(match.group(1).split()).casefold()
            if label in definitions:
                line_number = text.count("\n", 0, match.start()) + 1
                fail(f"{relative(path)}:{line_number}: duplicate Markdown reference")
            definitions.add(label)
            targets.append((match.group(2), match.start()))

        for match in MARKDOWN_REFERENCE_USE_RE.finditer(text):
            raw_label = match.group(2) or match.group(1)
            label = " ".join(raw_label.split()).casefold()
            if label not in definitions:
                line_number = text.count("\n", 0, match.start()) + 1
                fail(f"{relative(path)}:{line_number}: undefined Markdown reference")

        for match in HTML_LINK_RE.finditer(text):
            raw_target = next(value for value in match.groups() if value is not None)
            targets.append((raw_target, match.start()))

        for raw_target, position in targets:
            target, forbidden_scheme = local_link_target(raw_target)
            if target is None:
                continue
            line_number = text.count("\n", 0, position) + 1
            portable_target = target.replace("\\", "/")
            if forbidden_scheme or Path(portable_target).is_absolute():
                fail(f"{relative(path)}:{line_number}: unsafe absolute local link")
                continue
            resolved = (path.parent / portable_target).resolve()
            try:
                resolved.relative_to(ROOT)
            except ValueError:
                fail(f"{relative(path)}:{line_number}: local link escapes repository")
                continue
            if not resolved.exists():
                fail(f"{relative(path)}:{line_number}: broken local Markdown link")
    pass_if_clean("repository-contained local Markdown links", previous_error_count)


def collect_rule_surface_files(root_files: list[Path]) -> list[Path]:
    previous_error_count = len(errors)
    files = set(root_files)
    for base in (ROOT / ".agents/skills", ROOT / "scripts"):
        if not base.exists():
            continue
        if uses_symlink_component(base):
            fail(f"{relative(base)}: rules-package directory must not use a symlink")
            continue
        for path in base.rglob("*"):
            if any(part in IGNORED_SURFACE_PARTS for part in path.relative_to(ROOT).parts):
                continue
            if path.is_symlink():
                fail(f"{relative(path)}: symlinks are not allowed in the rules package")
            elif path.is_file():
                files.add(path)
    pass_if_clean("rules-package surface discovery", previous_error_count)
    return sorted(files)


def collect_module_guides() -> list[Path]:
    """Find publishable nested AGENTS.md files without traversing ignored trees."""
    previous_error_count = len(errors)
    try:
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "-co",
                "--exclude-standard",
                "-z",
                "--",
                ":(glob)**/AGENTS.md",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(
            "cannot discover nested repository guides "
            f"({exc.__class__.__name__}; details suppressed)"
        )
        return []
    if result.returncode:
        fail(
            "nested-guide discovery failed with exit code "
            f"{result.returncode}; command output suppressed"
        )
        return []

    guides: list[Path] = []
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        path = ROOT / item.decode("utf-8", errors="surrogateescape")
        if uses_symlink_component(path):
            fail(f"{relative(path)}: repository guide must not use a symlink")
        elif path.is_file():
            guides.append(path)
    pass_if_clean("tracked and untracked repository-guide discovery", previous_error_count)
    return sorted(set(guides))


def validate_text_whitespace(surface_files: list[Path]) -> None:
    previous_error_count = len(errors)
    for path in surface_files:
        text = read_text_resource(path)
        if text is None:
            continue
        if text and not text.endswith("\n"):
            fail(f"{relative(path)}: missing final newline")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.endswith((" ", "\t")):
                fail(f"{relative(path)}:{line_number}: trailing whitespace")
    pass_if_clean("rules-package text whitespace", previous_error_count)


def validate_secrets(surface_files: list[Path]) -> None:
    previous_error_count = len(errors)
    for path in surface_files:
        data = read_resource_bytes(path)
        if data is None:
            continue
        text = data.decode("latin-1")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                fail(f"{relative(path)}: possible {label}; value not printed")
    pass_if_clean(
        "common credential patterns across rules-package content",
        previous_error_count,
    )


def validate_git_diff() -> None:
    previous_error_count = len(errors)
    commands = (
        ("unstaged", ["git", "diff", "--check"]),
        ("staged", ["git", "diff", "--cached", "--check"]),
    )
    for label, command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            fail(
                f"cannot run {label} Git whitespace check "
                f"({exc.__class__.__name__}; details suppressed)"
            )
            continue
        if result.returncode:
            fail(
                f"{label} Git whitespace check failed with exit code "
                f"{result.returncode}; command output suppressed"
            )
    pass_if_clean("staged and unstaged Git whitespace", previous_error_count)


def changed_rule_paths(staged: bool) -> set[str] | None:
    command = ["git", "diff", "--no-ext-diff"]
    if staged:
        command.append("--cached")
    command.extend(["--name-only", "-z", "--", *RULE_PATHSPECS])
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(
            "cannot inspect staged rules-package consistency "
            f"({exc.__class__.__name__}; details suppressed)"
        )
        return None
    if result.returncode:
        fail(
            "rules-package staged-state inspection failed with exit code "
            f"{result.returncode}; command output suppressed"
        )
        return None
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def untracked_rule_paths() -> set[str] | None:
    try:
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "-o",
                "--exclude-standard",
                "-z",
                "--",
                *RULE_PATHSPECS,
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(
            "cannot inspect untracked rules-package state "
            f"({exc.__class__.__name__}; details suppressed)"
        )
        return None
    if result.returncode:
        fail(
            "rules-package untracked-state inspection failed with exit code "
            f"{result.returncode}; command output suppressed"
        )
        return None
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def validate_rules_index_consistency() -> None:
    """Do not certify working-tree text when a different version is staged."""
    previous_error_count = len(errors)
    staged = changed_rule_paths(staged=True)
    unstaged = changed_rule_paths(staged=False)
    untracked = untracked_rule_paths()
    working_tree = None
    if unstaged is not None and untracked is not None:
        working_tree = unstaged.union(untracked)
    if staged and working_tree:
        fail(
            "rules-package changes are split between the index and working tree; "
            "semantic validation requires one coherent rules snapshot"
        )
    pass_if_clean("rules-package staged/worktree consistency", previous_error_count)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template",
        action="store_true",
        help=(
            "also require and cross-check this template's bilingual README, "
            "license, and source register"
        ),
    )
    return parser.parse_args()


def main(template_mode: bool = False) -> int:
    root_files = validate_root_files(template_mode)
    validate_license_and_attributions(template_mode)
    skill_markdown, discovered = validate_skills()
    validate_routing(discovered, template_mode)
    validate_claude_import()

    surface_files = collect_rule_surface_files(root_files + collect_module_guides())
    markdown_files = [path for path in surface_files if path.suffix.lower() == ".md"]
    validate_local_links(markdown_files)
    validate_text_whitespace(surface_files)
    validate_secrets(surface_files)
    validate_rules_index_consistency()
    validate_git_diff()

    for check in checks:
        print(f"[OK] {check}")
    if errors:
        print(f"\n{len(errors)} validation error(s):", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    mode = "template" if template_mode else "portable"
    print(f"\n{mode.capitalize()} rules validation passed with {len(checks)} checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parse_args().template))
