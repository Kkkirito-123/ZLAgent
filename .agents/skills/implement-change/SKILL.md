---
name: "implement-change"
description: "Implement one sufficiently defined and bounded coding slice with minimal, surgical, goal-driven changes. Use during an approved deliver-change workflow or directly for a localized low-risk behavior fix that needs no requirement approval and already has an exact target, allowed scope, compatibility boundary, and success check. Do not use as the top-level workflow for a complete feature, refactor, or multi-file delivery, or for raw requirements, read-only review, trivial typo-only edits, guide synchronization, or publication."
---

# Implement Change

Produce the least code that satisfies one bounded objective while preserving
unrelated behavior and user work.

## Preconditions

Require a single goal, observable success condition, allowed scope, and known
compatibility or safety boundaries. If any missing decision would materially
change behavior or scope, stop and return to `$define-requirement`; do not guess.

## Workflow

1. Read the root and closest `AGENTS.md`, Git status, the owning implementation,
   contracts, tests, and relevant documentation. Identify unrelated local work
   that must remain untouched.
2. Establish the behavioral baseline before editing:
   - reproduce the bug or run the closest existing test when practical
   - for new behavior, state the observable before-and-after condition
   - record any environment limitation that prevents a trustworthy baseline
3. Locate the smallest justified change point by narrowing from component to
   candidate files, symbols and tests, call or data flow, and finally the owning
   behavior. Search with repository-native tools; do not choose a file from its
   name alone.
4. State assumptions that affect the implementation. If two reasonable
   interpretations remain and lead to materially different behavior, stop for a
   decision. Otherwise take the smallest reversible path.
5. Implement the bounded slice using existing patterns:
   - write straightforward code before introducing an abstraction
   - preserve public behavior and compatibility unless the contract changes it
   - avoid unrequested options, fallback paths, dependencies, refactors,
     formatting, renames, and future-proofing
   - modify neighboring code only when required for correctness
   - remove only dead code or artifacts created by this change
6. Add or update focused tests in proportion to risk and project practice. For a
   bug, prefer a regression check that fails before the fix and passes after it
   when feasible; do not impose universal TDD where another form of evidence is
   more appropriate.
7. Run the narrowest useful check after the slice. Classify a failure as
   implementation, environment, validation-path, or tool failure before
   retrying. Stop after three materially identical failures instead of widening
   scope by trial and error.
8. Review every changed line in context. Confirm that each line maps to the
   bounded objective, no unrelated behavior changed, and tests would detect the
   intended regression where applicable.
9. Return the changed files, focused checks and results, assumptions,
   discoveries, and unverified areas to the calling workflow. When this Skill
   was invoked directly, also apply the root guide-sync rule: use
   `$sync-project-guide` if durable or user-facing facts may have changed, or
   record `GUIDE_NO_UPDATE` and `README_NO_UPDATE` with reasons.

## Boundaries

- Do not broaden the objective, overwrite user work, expose credentials, or
  perform destructive or external actions without separate authority.
- Do not silence an error, weaken a test, or add a permissive fallback merely to
  obtain a green result.
- Do not claim provider, database, device, integration, or end-to-end validation
  from mocks, static analysis, or plausible output.
- Prefer comments that explain a non-obvious reason or invariant; make ordinary
  behavior clear in the code itself.

## Completion Evidence

Report the objective implemented, baseline, files changed, focused checks and
fresh results, assumptions, deviations from the expected scope, unverified
behavior, remaining risk, and—when invoked directly—the guide and README sync
decisions.
