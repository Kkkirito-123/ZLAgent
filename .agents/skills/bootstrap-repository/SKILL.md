---
name: "bootstrap-repository"
description: "Inspect a repository created from this template and replace template-only context with a concise, verified project-specific operating map while retaining the personal engineering contract and repository Skills. Use for the first approved repository bootstrap or when the user explicitly asks to establish the real product overview, architecture, directory responsibilities, commands, boundaries, and validation. This specialized workflow takes precedence over deliver-change; if bootstrap is not approved, use define-requirement first. Do not use for routine changes after bootstrap."
---

# Bootstrap Repository

Turn the generic template into an accurate operating map for the real repository
without discarding the reusable personal engineering baseline.

## Workflow

1. Require an approved bootstrap contract. If the scope is not yet approved, use
   `$define-requirement` or read
   `.agents/skills/define-requirement/SKILL.md`, present the decision-ready
   contract, and stop.
2. Read the root and applicable nested `AGENTS.md`. Inspect Git status,
   manifests, source, tests, CI, scripts, and existing documentation before
   describing the project.
3. Identify only verified facts that help an agent work safely:
   - repository positioning and users
   - high-level architecture and primary request, data, event, or execution flow
   - existing directories, entry points, and module responsibilities
   - canonical setup, development, run, build, migration, and validation commands
   - configuration sources, precedence, and important defaults
   - data and storage ownership, schema boundaries, and migration responsibility
   - repository workflow, Agent or Skill routing, and critical quality gates
   - dependency, protocol, compatibility, security, generated-code, license,
     distribution, or runtime boundaries
   - sensitive areas and runtime truths that are easy to miss
4. Rewrite the root guide as one coherent project document:
   - preserve the operative intent of authority, working-contract, Skill-routing,
     architecture-sync, evidence, and publication boundaries
   - remove template-only bootstrap instructions after they have served their
     purpose
   - replace the current-template map with verified project positioning,
     architecture, flow, directory responsibilities, commands, boundaries, and
     validation
   - omit headings with no useful verified content
5. Retain `.agents/skills/` as the repository workflow layer. Adapt a Skill only
   when a real project constraint requires it; do not delete the personal
   baseline merely because bootstrap is complete.
6. Keep `CLAUDE.md` as a thin import of the root authority. Synchronize retained
   translations without changing the English authority. Add a tool-native
   adapter only when the project uses and verifies it; do not duplicate Skill
   bodies across client-specific directories.
7. Preserve the root `LICENSE` copyright and permission notice for retained
   template material. Retain and update `ATTRIBUTIONS.md` while the copied rules
   package or its referenced interoperability and design sources remain in the
   project. A different project-wide license, additional copyright holder, or
   removal of a notice requires an explicit maintainer decision and a
   compatibility review; never present third-party work as relicensed by the
   derived project.
8. Replace template-specific README content with the real product overview and
   quick start after those facts are confirmed. Remove design-reference details
   that no longer describe retained material, but keep the README's current
   license and attribution links accurate.
9. Run available repository validation and inspect the final Diff. This template
   uses `python3 scripts/validate-rules.py --template`; after replacing its
   template-only adoption files, retain and document the portable
   `python3 scripts/validate-rules.py` mode plus the real project's quality
   gates. Use `$sync-project-guide` as the final checkpoint for the bootstrap
   changes within their approved file scope; bootstrap remains responsible for
   the integrated result. If no vertical slice exists yet, require the first
   delivery to re-inspect the landed implementation and run its normal sync
   checkpoint before completion.

## Boundaries

- Do not invent commands, architecture, owners, modules, future directories, or
  validation results.
- Do not create empty documentation trees, scripts, roadmaps, decisions, PRDs,
  or status files for possible future use.
- Preserve unrelated user work and never copy secrets, private endpoints,
  generated output, caches, or runtime data into guidance.
- Prefer a short operating map over a generic handbook. Add a module guide only
  for a subtree with distinct technology, commands, ownership, architecture, or
  risk.
- Put user-facing setup and behavior in `README.md`; keep current agent-facing
  architecture and safety boundaries in the closest `AGENTS.md`.

## Completion Evidence

Report the files changed, facts established and their evidence, checks actually
run, omitted sections, unverified areas, remaining risks, and the planned
architecture-sync checkpoint for the first vertical slice.
