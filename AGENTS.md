# ZLAgent Repository Guide

This file is the repository-wide source of truth for Codex, Claude Code, and
other coding agents. Read it before planning or editing.

- `AGENTS.md` is the normative English guide.
- `AGENTS.zh-CN.md` is the human-readable Chinese translation and must remain
  semantically aligned with this file.
- `CLAUDE.md` is only a conflict-free thin import of this guide.
- A nested `AGENTS.md`, if added later, overrides this file only within its
  own subtree. No nested repository guide exists today.
- This rules package is adapted from the audited Vibe Coding Rules revision
  recorded in `ATTRIBUTIONS.md`. Keep that source register current while the
  derived guides, Skills, or validator remain.

Default user-facing replies, plans, development notes, and retrospectives are
Chinese. Source files and documentation use UTF-8.

## Core Working Contract

Act as an engineering collaborator, not only as a code generator. Make work
manageable, reviewable, testable, and reusable.

- Serve one explicit objective at a time. Do not expand the request
  automatically.
- Before editing, inspect the owning implementation, tests, contracts,
  documentation, repository guide, and current Git state.
- Preserve unrelated and user-authored changes. Never overwrite, revert,
  delete, stage, commit, push, or publish them without explicit authorization.
- Prefer the smallest reversible change that satisfies the accepted scope.
- Do not add speculative layers, dependencies, directory trees, status files,
  or public claims.
- Keep secrets, credentials, private endpoints, local databases, generated
  output, and runtime state out of source control.
- Treat tool and external actions as permissioned operations with explicit
  error, evidence, side-effect, and trace boundaries.
- Validate in proportion to risk. State clearly what was not verified and why.

Before a new feature, refactor, repository bootstrap, deletion, batch edit,
dependency install or upgrade, global configuration change, database/schema
change, public API change, or security-boundary change, present a plan and wait
for approval. The plan must contain:

- objective;
- users and stakeholders;
- MVP scope;
- non-goals;
- expected file structure or change surface;
- acceptance criteria;
- risks and trade-offs;
- migration or rollback path when compatibility or durable state is involved.

Resolve low-risk ambiguity from repository evidence. If an assumption is
reversible and does not materially change scope, make the narrowest assumption
and report it. Stop for user direction when the choice changes product
behavior, security, external state, compatibility, cost, or accepted scope.

## Route Work Through Repository Skills

The repository workflows live under `.agents/skills/`. Read the selected
`SKILL.md` completely before acting, follow its checkpoints, and use the
smallest set that covers the request.

- `$define-requirement`: clarify an incomplete or ambiguous requirement and
  produce the approval-ready objective, stakeholders, MVP, non-goals, file
  scope, acceptance criteria, and risks.
- `$bootstrap-repository`: establish or refresh repository-level agent rules,
  thin adapters, validation, and baseline workflow files.
- `$deliver-change`: orchestrate a complete approved change from intake through
  implementation, guide synchronization, verification, and handoff.
- `$implement-change`: inspect, implement, test, and audit an approved code or
  documentation change without publishing it.
- `$sync-project-guide`: decide whether `AGENTS.md`, nested guides,
  `AGENTS.zh-CN.md`, or `README.md` must change after implementation.
- `$publish-change`: perform separately authorized staging, commit, push, or PR
  publication. It must never be inferred from implementation approval.

Classify work before execution:

- L0: read-only inspection, explanation, focused tests, or an obvious local bug
  with no interface, dependency, schema, permission, or architecture impact.
  It may proceed directly under this guide.
- L1: a bounded implementation or documentation change with local impact. Use
  `implement-change` for a small single-surface edit; use `deliver-change` for
  a complete feature, refactor, multi-file delivery, or end-to-end handoff.
- L2: architecture, protocol, permission, schema, dependency, public API,
  deletion, batch rewrite, or repository-baseline work. Use
  `define-requirement` when needed, obtain approval, then use
  `deliver-change` plus the relevant specialist workflow.

Use `bootstrap-repository` only for repository baseline work. Use
`sync-project-guide` whenever implementation changes architecture facts,
operating commands, validation requirements, or public behavior. Use
`publish-change` only after separate explicit publication authorization.

## Repository Positioning

ZLAgent (`Kkkirito-123/ZLAgent`) is an IM-first personal assistant product. It
accepts messages from personal Weixin, WeCom Bot, Webhook, HTTP, and scheduled
jobs; enriches each turn with skills, memory, and knowledge; lets an LLM select
bounded tools; and sends the result through the originating delivery channel.

Its engineering purpose is to learn and validate Agent Harness practices inside
a real personal-assistant product: capability discovery, host-granted
permission, execution, structured failure, evidence, side effects, and trace
must form one inspectable path. Hermes Agent and OpenClaw are product and
engineering references for extensible personal assistants, not feature
checklists or code sources to clone.

This repository is not OpenZLAgent. OpenZLAgent is a separate generic Harness
project and may be used as a read-only engineering reference. Do not copy its
product structure or code wholesale into ZLAgent, and do not claim that
ZLAgent already implements OpenZLAgent's durable task lifecycle.

Default engineering direction:

- establish ownership and dependency boundaries before adding behavior;
- use explicit checkpoints and acceptance criteria for complex work;
- keep model judgment separate from host-granted authority;
- prefer evidence-backed completion over natural-language success claims;
- compare with mature open-source agent practices such as OpenZLAgent,
  DeerFlow, and OpenGUI only where they fit this product.

## Main Flow and Dependency Direction

```text
FastAPI lifespan -> bootstrap composition root
gateway / API / cron -> AgentLoop
AgentLoop -> turn preparation -> tool loop -> post-turn pipeline
tool loop / confirmation / operator surfaces -> HarnessExecution
HarnessExecution -> permission-aware ToolRegistry -> built-in or MCP tool
memory / skills / wiki / graph / storage -> durable personal-assistant state
```

Ownership rules:

- `backend/app.py` owns process lifecycle only. Object construction and wiring
  belong in `backend/bootstrap/`.
- `backend/gateways/` normalizes inbound messages and outbound delivery. It
  must not execute tools, grant permission, or decide task completion.
- `backend/agent/` owns one conversational turn, model/tool iteration,
  confirmation suspension, context preparation, and post-turn policies.
- `backend/harness/` owns the explicit per-call execution boundary,
  observability, progress reporting, and extension-management boundaries. It
  must not import gateway or FastAPI code.
- `backend/tools/` owns tool contracts, registry, permission metadata, and
  concrete built-in capabilities. Tool execution outside `HarnessExecution`
  is forbidden except inside `ToolRegistry`, the raw invocation boundary used
  by the harness.
- `backend/memory/`, `backend/skills/`, `backend/wiki/`, `backend/graph/`,
  `backend/mcp/`, `backend/cron/`, and `backend/domains/` are product
  capabilities. Do not relocate them merely to make the tree look uniform.
- `backend/db/` and `backend/storage/` must not own or depend on agent
  orchestration.

`HarnessExecution` is the single execution lifecycle for an individual tool
call. `AgentLoop` remains the conversational-turn lifecycle. At present,
ZLAgent does not provide OpenZLAgent's immutable durable `TaskContract`, durable
plan/event/checkpoint ledger, acceptance gate, worker lifecycle, or transactional
outbox. Do not make exactly-once or durable-completion claims based only on
traces or structured tool results.

## Tool and External-Action Boundaries

- Safe tools are read-only. A tool that mutates files, memory, skills,
  schedules, installed software, delivery targets, or external systems must be
  confirm-tier.
- A read-only action on a confirm-tier multi-action tool may bypass
  confirmation only through `Tool.is_action_read_only()`.
- Trusted background review may use explicit host-granted confirmation. Model
  output may never grant its own permission.
- Confirmation resume, prefetch, extension deletion, HTTP tool testing, and
  ordinary model tool calls must all use `HarnessExecution`.
- Every tool result carries structured status, error, recoverability, next
  action where applicable, source, evidence, and side-effect metadata.
  Natural-language `content` is not audit evidence. Trace metadata is recorded
  separately by the Harness observability path.
- Arguments stored in traces must be redacted. Logs, metrics, progress views,
  and operator surfaces must never decide whether a user request succeeded.
- Apply read-before-write where a safe inspection can reduce mutation risk.
- Record uncertain external outcomes honestly. Do not retry a non-idempotent
  side effect blindly, and do not claim durable exactly-once behavior.

## Runtime, Data, and Configuration Truths

- The supported runtime is Python 3.11+.
- Docker starts `zlagent`, PostgreSQL/pgvector, and Redis. Neo4j is not a
  runtime dependency.
- Local development may use the configured local storage path; Docker uses
  PostgreSQL and Redis according to the checked-in compose and environment
  configuration. Inspect `backend/core/config.py` before changing defaults.
- MCP is optional and can be disabled with `ZLAGENT_MCP_ENABLED=false`.
- Without an LLM key, the service starts with an echo fallback.
- Personal Weixin is the only IM path the maintainer has reported as fully
  end-to-end verified. WeCom Bot and generic Webhook remain partial paths.
- `.env`, `data/`, runtime workspace state, credentials, logs, caches, local
  databases, and generated artifacts are not source authority and must not be
  committed.

## Sensitive Areas

- `backend/agent/loop.py` and `backend/agent/tool_loop/`
- `backend/agent/confirmation/`
- `backend/harness/execution.py`
- `backend/tools/permission.py` and `backend/tools/registry.py`
- mutation tools including `code_execution.py`, `mcp_manage.py`,
  `send_message.py`, `skill_manage.py`, and file or knowledge write tools
- `backend/mcp/installer.py` and MCP lifecycle or transport code
- `backend/gateways/weixin.py` and vendored iLink protocol code
- database migrations, delivery-target handling, secrets, and authentication

Protocol, permission, schema, provider, dependency, public API, and durable
state changes require an approved plan plus a migration or rollback path.

## Canonical Commands

```bash
python -m pip install -e '.[dev]'
python3 scripts/test_validate_rules.py
python3 scripts/validate-rules.py
python -m compileall -q backend scripts tests
python -m unittest discover -s tests
ruff check backend scripts tests
ZLAGENT_DEMO_FAST=1 ZLAGENT_MCP_ENABLED=false python scripts/demo_e2e.py
python scripts/demo_capability_lifecycle.py
docker compose config --quiet
docker compose up -d --build
```

Focused MCP and GraphRAG scripts may require subprocesses, network access,
provider credentials, or a running service. Report those checks separately;
do not present them as offline unit tests.

## Keep Architecture and Public Guidance Current

After every implementation, classify guide synchronization explicitly:

- `GUIDE_UPDATED`: architecture, ownership, dependency direction, protocol,
  permission/security boundary, canonical command, validation requirement, or
  durable operating fact changed and the owning `AGENTS.md` files were updated.
- `GUIDE_NO_UPDATE`: none of those guide-owned facts changed; state why.
- `GUIDE_UPDATE_REQUIRED`: the implementation changed guide-owned facts but
  the guide cannot yet be updated. This blocks completion.

Classify README synchronization separately:

- `README_UPDATED`: user-facing setup, behavior, support status, or operating
  instructions changed and `README.md` was updated.
- `README_NO_UPDATE`: no public-facing fact changed; state why.
- `README_UPDATE_REQUIRED`: public behavior changed but documentation cannot
  yet be updated. This blocks completion.

Update this guide in the same change whenever package ownership, main flow,
dependency direction, protocol, permission/security boundary, canonical
commands, or validation requirements change. Keep `AGENTS.zh-CN.md`
semantically synchronized. Update `README.md` when user-facing setup or
behavior changes. Do not publish volatile task status, temporary paths, local
machine details, or unverified claims as durable architecture.

## Evidence-Based Delivery

- Add tests in proportion to risk. Mocks do not prove live Weixin, provider,
  database, MCP, or external side-effect behavior.
- Run focused checks first, then the canonical offline suite appropriate to the
  changed surface.
- Before completion, inspect the final diff for credentials, private endpoints,
  generated files, unrelated edits, compatibility breaks, debug artifacts, and
  whitespace errors.
- Do not stage, commit, push, open a PR, merge, release, or deploy unless that
  publication action was separately authorized.

The final Chinese report must state:

- what changed;
- files changed;
- validation performed and evidence obtained;
- discoveries or root cause;
- remaining risks and unverified paths;
- `GUIDE_*` and `README_*` synchronization status;
- any genuinely reusable rule worth retaining.
