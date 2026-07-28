#!/usr/bin/env python3
"""Regression tests for validate-rules.py security and portability boundaries."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock


VALIDATOR_PATH = Path(__file__).with_name("validate-rules.py")
SPEC = importlib.util.spec_from_file_location("validate_rules", VALIDATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load validate-rules.py")
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class ValidatorRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        VALIDATOR.errors.clear()
        VALIDATOR.checks.clear()
        VALIDATOR.resource_bytes_cache.clear()

    def test_frontmatter_requires_canonical_quoted_strings(self) -> None:
        path = VALIDATOR.ROOT / "probe.md"
        valid = (
            '---\nname: "probe-skill"\n'
            'description: "A canonical description."\n---\n'
        )
        self.assertEqual(
            VALIDATOR.parse_frontmatter(path, valid),
            {"name": "probe-skill", "description": "A canonical description."},
        )
        self.assertFalse(VALIDATOR.errors)

        invalid_values = ("plain: scalar", ">", '"unterminated')
        for value in invalid_values:
            with self.subTest(value=value):
                self.setUp()
                text = f'---\nname: "probe-skill"\ndescription: {value}\n---\n'
                VALIDATOR.parse_frontmatter(path, text)
                self.assertTrue(VALIDATOR.errors)

    def test_metadata_rejects_wrong_parent_and_non_boolean_policy(self) -> None:
        path = VALIDATOR.ROOT / "probe.yaml"
        wrong_parent = (
            "wrong_policy:\n"
            "  allow_implicit_invocation: false\n"
            "interface:\n"
            '  display_name: "Probe Skill"\n'
            '  short_description: "A sufficiently long probe description"\n'
            '  default_prompt: "Use $probe-skill for this probe."\n'
        )
        metadata = VALIDATOR.parse_openai_metadata(path, wrong_parent)
        self.assertTrue(VALIDATOR.errors)
        self.assertNotIn("policy", metadata)

        self.setUp()
        string_policy = (
            "interface:\n"
            '  display_name: "Probe Skill"\n'
            '  short_description: "A sufficiently long probe description"\n'
            '  default_prompt: "Use $probe-skill for this probe."\n'
            "policy:\n"
            '  allow_implicit_invocation: "false"\n'
        )
        metadata = VALIDATOR.parse_openai_metadata(path, string_policy)
        self.assertTrue(VALIDATOR.errors)
        self.assertNotIn("allow_implicit_invocation", metadata["policy"])

    def test_claude_adapter_rejects_extra_instructions(self) -> None:
        conflicting = (
            "# Claude Code Instructions\n\n"
            "Use a different authority.\n\n@AGENTS.md\n"
        )
        with mock.patch.object(VALIDATOR, "read_text", return_value=conflicting):
            VALIDATOR.validate_claude_import()
        self.assertTrue(VALIDATOR.errors)

    def test_template_license_and_attribution_contract(self) -> None:
        valid_attributions = (
            "Apart from standard license notices reproduced for their intended "
            "purpose\n"
            + "\n".join(VALIDATOR.TEMPLATE_ATTRIBUTION_SOURCES)
        )
        valid_readme = (
            "[MIT License](LICENSE)\n"
            "[ATTRIBUTIONS.md](ATTRIBUTIONS.md)\n"
        )

        def valid_text(path: Path) -> str:
            if path.name == "LICENSE":
                return VALIDATOR.MIT_LICENSE_TEXT
            if path.name == "ATTRIBUTIONS.md":
                return valid_attributions
            if path.name in {"README.md", "README.zh-CN.md"}:
                return valid_readme
            return ""

        with mock.patch.object(VALIDATOR, "read_text", side_effect=valid_text):
            VALIDATOR.validate_license_and_attributions(template_mode=True)
        self.assertFalse(VALIDATOR.errors)

        self.setUp()

        def invalid_text(path: Path) -> str:
            if path.name == "LICENSE":
                return "MIT-like text is not the canonical license\n"
            if path.name == "ATTRIBUTIONS.md":
                return valid_attributions.replace(
                    VALIDATOR.TEMPLATE_ATTRIBUTION_SOURCES[-1], ""
                )
            return valid_text(path)

        with mock.patch.object(VALIDATOR, "read_text", side_effect=invalid_text):
            VALIDATOR.validate_license_and_attributions(template_mode=True)
        self.assertTrue(any("canonical approved MIT" in e for e in VALIDATOR.errors))
        self.assertTrue(any("audited source records" in e for e in VALIDATOR.errors))

    def test_local_link_boundaries_and_reference_definitions(self) -> None:
        cases = (
            ("[x][missing]\n", "undefined Markdown reference"),
            ("![alt][missing]\n", "undefined Markdown reference"),
            ('<a href="../../../../etc/passwd">x</a>\n', "escapes repository"),
            ("[x](C:Windows\\win.ini)\n", "unsafe absolute local link"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.setUp()
                with mock.patch.object(VALIDATOR, "read_text", return_value=text):
                    VALIDATOR.validate_local_links([VALIDATOR.ROOT / "probe.md"])
                self.assertTrue(any(expected in error for error in VALIDATOR.errors))

        self.setUp()
        valid = "[guide][root]\n[root]: AGENTS.md\n"
        with mock.patch.object(VALIDATOR, "read_text", return_value=valid):
            VALIDATOR.validate_local_links([VALIDATOR.ROOT / "probe.md"])
        self.assertFalse(VALIDATOR.errors)

    def test_route_detection_ignores_normal_variables(self) -> None:
        text = "Use $define-requirement; keep $port and $project_root unchanged."
        with mock.patch.object(VALIDATOR, "read_text", return_value=text):
            refs = VALIDATOR.skill_references(
                VALIDATOR.ROOT / "probe.md", {"define-requirement"}
            )
        self.assertEqual(refs, {"define-requirement"})

    def test_split_index_and_worktree_rule_changes_are_rejected(self) -> None:
        changed = mock.patch.object(
            VALIDATOR,
            "changed_rule_paths",
            side_effect=lambda staged: {"AGENTS.md"} if staged else {"README.md"},
        )
        untracked = mock.patch.object(
            VALIDATOR, "untracked_rule_paths", return_value=set()
        )
        with changed:
            with untracked:
                VALIDATOR.validate_rules_index_consistency()
        self.assertTrue(any("one coherent rules snapshot" in e for e in VALIDATOR.errors))

    def test_failure_output_suppresses_command_content_and_path_tokens(self) -> None:
        marker = "ghp_" + "A" * 30
        result = subprocess.CompletedProcess(
            args=["git"], returncode=1, stdout=marker, stderr=marker
        )
        with mock.patch.object(VALIDATOR.subprocess, "run", return_value=result):
            VALIDATOR.validate_git_diff()
        self.assertTrue(VALIDATOR.errors)
        self.assertTrue(all(marker not in error for error in VALIDATOR.errors))

        displayed = VALIDATOR.relative(VALIDATOR.ROOT / f"bad-{marker}.md")
        self.assertNotIn(marker, displayed)
        self.assertIn("<redacted>", displayed)


if __name__ == "__main__":
    unittest.main()
