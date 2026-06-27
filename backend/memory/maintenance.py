from __future__ import annotations

from loguru import logger

from .manager import MemoryManager
from .store import MemoryStore
from ..skills.curator import SkillCurator


class MemoryMaintenance:
    def __init__(self, memory_store: MemoryStore, memory_manager: MemoryManager, skill_curator: SkillCurator) -> None:
        self._memory_store = memory_store
        self._memory_manager = memory_manager
        self._skill_curator = skill_curator

    def run(self) -> dict[str, int]:
        compacted_memories = self._memory_store.compact()
        compacted_skills = self._skill_curator.compact()
        logger.info(
            "[maintenance] memories={} skills={}",
            compacted_memories,
            compacted_skills,
        )
        return {
            "memories": compacted_memories,
            "skills": compacted_skills,
        }
