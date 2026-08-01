"""Skill manifest types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SkillFormat(str, Enum):
    """Supported skill file formats."""

    # Keep the historical serialized value so existing inventories remain
    # compatible while exposing the ecosystem-standard name to new callers.
    AGENT_SKILLS = "hermes"
    HERMES = AGENT_SKILLS
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """Metadata for a reusable workflow."""

    id: str
    name: str
    description: str = ""
    version: str = "0.1.0"
    license: str | None = None
    compatibility: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    triggers: tuple[str, ...] = field(default_factory=tuple)
    format: SkillFormat = SkillFormat.AGENT_SKILLS
    root: Path | None = None
    body_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("skill.id must be non-empty")
        if not self.name.strip():
            raise ValueError("skill.name must be non-empty")
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "triggers", tuple(self.triggers))
        object.__setattr__(self, "metadata", dict(self.metadata))
