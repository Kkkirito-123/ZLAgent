---
name: "sync-project-guide"
description: "Inspect final change evidence and decide separately whether root or module Agent guidance and user-facing README content need synchronization. Use for an explicit guidance check, an explicitly authorized guide or README update, or the formal final stage of an approved delivery or bootstrap when layout, ownership, architecture, flows, commands, configuration, data or storage, contracts, compatibility, security, workflow or Skill routing, quality gates, generated-code ownership, license or distribution boundaries, runtime behavior, setup, or user behavior may have changed. Edit only within the authorized file scope; otherwise report the required update. Do not trigger from an isolated completion statement or use for changelogs, temporary status, future plans, or internal refactors that leave relevant facts unchanged."
---

# Sync Project Guide

Keep `AGENTS.md` aligned with how the repository works now without turning it
into a changelog, backlog, or copy of implementation detail.

## Workflow

1. Establish the mode and exact file scope before editing:
   - `CHECK_ONLY`: inspect and report; do not modify files
   - `SYNC_AUTHORIZED`: an explicit update request or approved delivery/bootstrap
     contract authorizes only the named or plan-scoped guide and README files
   Then read Git status, the staged Diff, the unstaged Diff, the in-scope
   untracked files, the root guide, and every closest guide governing a changed
   area. Use source and executable tests for current behavior and an approved
   specification for intended behavior; report material drift.
2. Compare the change against these durable facts:
   - top-level layout, module ownership, service topology, or primary entry points
   - main request, data, event, or agent execution flow
   - dependency direction, public contracts, protocols, or compatibility rules
   - canonical setup, development, run, build, migration, or validation commands
   - configuration sources, precedence, or important defaults
   - data or storage ownership, schema boundaries, or migration responsibility
   - repository workflow, Agent or Skill routing, or critical quality gates
   - security boundaries, sensitive areas, generated-code ownership, or runtime
     behavior an agent must know before editing
   - license or distribution boundaries
3. Make two independent decisions; one change can require both surfaces:
   - no relevant change -> `GUIDE_NO_UPDATE` or `README_NO_UPDATE`
   - change found but editing is check-only or outside the authorized file scope
     -> `GUIDE_UPDATE_REQUIRED` or `README_UPDATE_REQUIRED`
   - change found and its files are authorized -> synchronize, then report
     `GUIDE_UPDATED` or `README_UPDATED`
4. For an authorized guide update, edit each closest guide that owns a changed
   fact. Also update the root guide when repository topology, cross-module flow,
   workflow, routing, or another repository-wide fact changes. Replace stale
   statements and record verified current behavior. Add a module guide only
   when a subtree has genuinely distinct commands, technology, architecture,
   ownership, or risk.
5. Synchronize every authorized retained human translation without changing the
   English authority. Keep `CLAUDE.md` as the exact thin import. Handle an
   authorized user-facing README update independently of the guide decision.
6. Report pre-existing or out-of-scope drift without repairing it. Never use a
   current sync task as authority for unrelated documentation cleanup.
7. Verify every mentioned path, entry point, and command against the repository.
   Inspect the guide Diff for stale claims, duplication, excessive detail, and
   conflicts with nearer rules.

## Boundaries

- Do not update a guide merely because files changed.
- A request to check, review, or report is not edit authorization. Do not modify
  a file excluded by the approved scope even when it needs an update.
- Do not add implementation trivia, timestamps, commit SHAs, temporary progress,
  TODOs, release history, speculative directories, or complete dependency lists.
- Do not create a fixed documentation hierarchy for a small repository.
- A nearer guide may refine local facts but must not silently weaken root safety,
  permission, or publication boundaries.
- If evidence is insufficient, report the uncertainty and leave the guide
  unchanged rather than inventing a fact.

## Completion Evidence

State both final guide and README statuses, identify the durable and user-facing
facts considered, list any authorized guide, translation, and README files
changed, identify required but unauthorized updates, cite the repository
evidence, and note any remaining uncertainty.
