from __future__ import annotations

from typing import Optional, Sequence

from loguru import logger

from ...skills.loader import SkillManifest


def pick_skill_for_message(
    message_text: str,
    manifests: Sequence[SkillManifest],
) -> Optional[str]:
    if not message_text or not manifests:
        return None
    text_lower = message_text.lower()

    candidates: list[tuple[int, int, str]] = []
    for manifest in manifests:
        if not manifest.triggers:
            continue
        hits = 0
        for trigger in manifest.triggers:
            if not trigger:
                continue
            if trigger.lower() in text_lower:
                hits += 1
        if hits == 0:
            continue
        candidates.append((hits, len(manifest.triggers), manifest.id))

    if not candidates:
        return None

    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    chosen = candidates[0][2]

    logger.info(
        "skill_router picked {!r} (hits={}, triggers_total={}, candidates={})",
        chosen,
        candidates[0][0],
        candidates[0][1],
        len(candidates),
    )
    return chosen
