---
name: "define-requirement"
description: "Classify and define an unapproved or ambiguous repository change before implementation. Use for an unapproved template bootstrap, feature, refactor, multi-file change, public API or compatibility decision, dependency or schema change, destructive action, PRD request, or other high-impact work that still needs a clear goal, users, MVP, non-goals, scope, acceptance criteria, risks, or approval. Do not use for read-only questions, diagnosis without implementation, a localized low-risk fix with an exact target and success check, or work that already has an approved change contract."
---

# Define Requirement

Turn a raw request into the smallest decision-ready change contract. Decide how
much specification is justified; do not make a permanent PRD the price of every
small change.

## Workflow

1. Read the root and applicable nested `AGENTS.md`. Inspect only enough existing
   implementation, contracts, tests, documentation, and task context to avoid
   planning from fiction. Do not edit implementation during requirement
   definition.
2. Classify the request:
   - **L0 — no requirement approval:** read-only work, diagnosis without a
     requested fix, a trivial mechanical repair, or a localized low-risk repair
     whose intended behavior, target, and success check are already clear. It
     must not cross a security, permission, public-contract, data-integrity,
     dependency, migration, destructive, or other high-impact boundary. Use the
     core contract directly for read-only or mechanical work and
     `$implement-change` for the behavioral repair; no PRD or full change
     contract is required.
   - **L1 — change contract:** a normal feature, refactor, multi-file fix,
     routine patch or minor dependency update, behavior-neutral lockfile
     maintenance, or other substantive change that needs one explicit approval
     before delivery. Keep the contract in the conversation by default; all
     dependency work still receives risk-based validation.
   - **L2 — durable specification:** cross-service or multi-session work, public
     API or schema changes, adding or removing a production dependency, a major
     or core-runtime upgrade, dependency changes with security, license, cost,
     compatibility, or migration effects, permission changes, data migration,
     destructive behavior, expensive providers, several stakeholders, or a
     decision that needs long-lived ownership.
3. Resolve only material ambiguity. When interpretations would change user
   behavior, public contracts, cost, security, or scope, list the alternatives
   and request a decision. Otherwise state the smallest reversible assumption.
4. Produce the change contract:
   - goal
   - users or stakeholders
   - MVP
   - non-goals
   - expected file or component scope
   - observable acceptance criteria
   - risks and trade-offs
   - assumptions, open decisions, and validation approach
5. Decide the artifact:
   - L0: no requirement artifact
   - L1: conversation contract unless the repository already has an approved
     source of truth that must be updated
   - L2: propose the existing Issue, specification, design, or tracker to update;
     if none exists, propose a path and contents, then wait for approval before
     creating it
6. Present `READY_FOR_APPROVAL` when the contract is complete, or `NEEDS_INPUT`
   with only the blocking decisions. Do not start implementation until required
   approval is explicit.
7. After approval, persist an L2 specification only when that artifact was part
   of the approved scope. If the user asked only for requirements or a PRD,
   report the artifact and stop. Otherwise route the approved objective without
   widening it:
   - first template bootstrap -> `$bootstrap-repository`
   - guide or README synchronization only -> `$sync-project-guide`
   - one localized low-risk coding slice -> `$implement-change`
   - substantive end-to-end implementation -> `$deliver-change`
   Read the corresponding `.agents/skills/<name>/SKILL.md` when native discovery
   is unavailable. Approval here never grants publication authority.

## Durable Specification Minimum

When L2 genuinely requires a persistent artifact, include the confirmed current
state, target behavior, users and scenarios, functional and non-functional
acceptance criteria, public interfaces or data effects, permission and security
boundaries, rollout or migration, rollback or recovery, risks, and unresolved
decisions. Reuse the repository's existing format and location; do not impose a
universal filename.

## Boundaries

- Do not invent product decisions, users, metrics, owners, architecture, paths,
  or commands.
- Do not write code, install dependencies, modify schemas, or perform external
  actions while approval is pending.
- Do not infer implementation authorization from approval to write a
  requirements-only artifact.
- Do not create empty PRDs, `TECH_SPEC.md`, `subtasks.json`, timelines, or
  documentation trees for possible future use.
- A user request to implement is not approval of choices that have not yet been
  exposed when repository rules require a plan checkpoint.

## Completion Evidence

Report the classification, contract or blocking decisions, sources inspected,
assumptions, proposed durable artifact if any, and the exact next approved
workflow.
