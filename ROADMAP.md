# re_zlagent Rebuild Roadmap

**Language:** [English](./ROADMAP.md) | [Simplified Chinese](./ROADMAP.zh-CN.md)

> Authority: this English file is the source of truth for delivery stages,
> status, entry gates, and exit gates. `ROADMAP.zh-CN.md` is a human-facing
> translation. AI agents must use this file and must not use the translation as
> an operational source.

## 1. Purpose

This roadmap turns the ZLAgent rebuild into a sequence of verifiable engineering
stages. It is not a feature wishlist and it is not a traditional product PRD.
It combines the minimum product frame, migration history, delivery order, and
acceptance gates needed to replace the old backend safely.

Document ownership is intentionally narrow:

- `CLAUDE.md` owns AI working rules and architecture invariants.
- `ROADMAP.md` owns stage order, stage status, and migration decisions.
- `README.md` owns the concise human-facing project overview and usage.
- `CONTRIBUTING.md` owns the development workflow.
- `*.zh-CN.md` files are synchronized human-readable translations, not AI
  authority.

Do not copy roadmap details back into `README.md` or `CLAUDE.md`. Link here
instead.

## 2. Product Frame

### Problem

The old ZLAgent backend contains useful behavior, but task truth, tool execution,
recovery, product adapters, and optional knowledge features are tightly coupled.
Long tasks can outlive chat context, external actions can be repeated after
failure, and model prose is too easy to confuse with verified completion.

### Users and stakeholders

- the repository owner operating a personal IM-first assistant
- maintainers adding tools, gateways, models, and storage adapters
- operators inspecting, pausing, resuming, cancelling, or forking runs
- coding agents contributing under explicit repository rules

### Target outcome

`re_zlagent` becomes a reusable agent harness that can accept a task, persist an
immutable task contract and plan, execute bounded tools, survive process restarts,
wait for users safely, resume all unfinished work, and complete only from trusted
acceptance evidence.

### First product MVP boundary

The first product-complete flow is:

```text
request
  -> trusted task contract
  -> validated plan
  -> bounded tool execution
  -> append-only events and checkpoints
  -> durable user wait or recoverable failure
  -> restart-safe continuation
  -> trusted acceptance
  -> structured response
```

The first MVP uses one process worker, one model adapter, SQLite durability, a
minimal submit/status/control surface, and the existing file/URL/message tool
boundaries. PostgreSQL production operation follows after the semantics are
proven locally.

### Explicit non-goals for the first MVP

- default DAG concurrency
- autonomous multi-agent delegation
- broad MCP, cron, or OpenGUI migration
- wiki, Graph-RAG, or geo knowledge systems
- copying the old backend module structure
- treating model output, benchmark scores, or health snapshots as completion
  authority

## 3. Status Model

| Status | Meaning |
| --- | --- |
| `LANDED` | Committed in the current branch history and validated at its original scope. |
| `LOCAL` | Implemented and validated in the worktree, but not committed. |
| `NEXT` | The only stage that should start next. |
| `PLANNED` | Ordered future work; do not start before all entry gates pass. |
| `DEFERRED` | Intentionally postponed and not part of the current critical path. |
| `REMOVED` | Explicitly excluded from the rebuild unless a new product decision reopens it. |

`LANDED` does not mean the implementation can never be corrected. A later audit
may reopen semantics without erasing the historical stage.

## 4. Engineering Invariants

Every stage must preserve these invariants:

1. `HarnessRuntime` is the only tool-execution lifecycle.
2. A run references immutable contract and plan revisions.
3. Models may propose plans and requirements, but may not assert trusted evidence,
   passed tests, freshness, or human approvals.
4. Events and checkpoints are append-only truth; run rows are projections.
5. Stage completion and task completion are separate decisions.
6. Recovery continues every required unfinished step; retrying one failed step is
   not sufficient by itself.
7. Chat history is never the authoritative long-task state.
8. External side effects require permission, an idempotency identity, durable
   intent, and inspectable outcome state.
9. Read models, progress views, scanners, evals, and health monitors never mutate
   execution state or decide completion.
10. Concurrent execution remains disabled until worker ownership, side-effect
    safety, deterministic merge, and fault-injection tests all exist.

## 5. Stage Overview

| Stage | Status | Objective |
| --- | --- | --- |
| M1 | `LANDED` | Freeze rebuild scope and repository authority. |
| M2 | `LANDED` | Establish harness, structured tools, task events, checkpoints, and acceptance. |
| M3 | `LANDED` | Establish application, gateway, operator, facade, and package boundaries. |
| M4 | `LANDED` | Add explicit approval and alternative recovery entry points. |
| M5 | `LANDED` | Add stage verification, failure classification, DAG expression, and eval foundations. |
| M6 | `LANDED` | Add long-task plans, projections, and restart context packs. |
| M7 | `LANDED` | Add the long-task ledger storage boundary. |
| M8 | `LANDED` | Add SQLite durability for interactions, artifacts, and side effects. |
| M9 | `LANDED` | Add read-only parked-run classification. |
| M10 | `LANDED` | Add conservative DAG execution assessment. |
| M11 | `LANDED` | Record runtime side-effect results in the ledger. |
| M12 | `LANDED` | Add the composite long-task progress snapshot. |
| M13 | `LANDED` | Close acceptance trust and immutable task-contract semantics. |
| M14 | `LANDED` | Persist plans and continue the complete unfinished frontier after recovery. |
| M15 | `LANDED` | Make external side effects crash-safe through an outbox boundary. |
| M16 | `LANDED` | Add durable worker claiming, leases, heartbeat, and retry budgets. |
| M17 | `LANDED` | Deliver a real task submission and execution MVP. |
| M18 | `LANDED` | Add CI, benchmarks, fault injection, and latency/reliability gates. |
| M19 | `DEFERRED` | Migrate selected optional capabilities one bounded slice at a time. |
| M20 | `DEFERRED` | Enable safe DAG concurrency after all prerequisite gates pass. |
| M21 | `LOCAL` | Promote the rebuild to root and close the approved legacy migration. |

## 6. Historical Stages

### M1 - Scope and repository authority

**Status:** `LANDED`

**Objective:** Create `re_zlagent/` as the only rebuild workspace and make old
backend, old tests, OpenGUI, and governance drafts reference-only.

**Delivered:** thin `AGENTS.md`, authoritative `CLAUDE.md`, package workspace,
focused source and test roots, and a no-blind-copy migration rule.

**Evidence:** commit `eeeb42f` and current repository boundary tests.

### M2 - Harness and task foundation

**Status:** `LANDED`

**Objective:** Establish deterministic contracts before adding product adapters.

**Delivered:** structured `ToolResult`, permissions, read-before-write, sandboxed
paths, task contracts, append-only events, checkpoints, recovery policies,
`AcceptanceGate`, SQLite task storage, and PostgreSQL task-store semantics.

**Original exit gate:** bounded tool execution and deterministic task acceptance
were covered by unit tests.

### M3 - Application and package boundaries

**Status:** `LANDED`

**Objective:** Prevent product surfaces from becoming alternate execution paths.

**Delivered:** `re_zlagent.*` namespace, app/gateway contracts, operator controls,
facade snapshots, bootstrap assembly, JSON CLI controls, and local check command.

**Evidence:** commits `f1f4dc1`, `9acea7d`, and `6af3fa5`.

### M4 - Explicit recovery entry points

**Status:** `LANDED`

**Objective:** Make user approval and alternative-tool recovery inspectable and
route them through the normal runtime and acceptance path.

**Delivered:** approval service, approval resume, explicit alternative tool
resume, failure checkpoints, and append-only recovery events.

**Evidence:** commits `df828c4` and `28e6055`.

### M5 - Verification, DAG, and evaluation foundations

**Status:** `LANDED`

**Objective:** Separate local step completion from final task completion and
express dependencies without enabling unsafe concurrency.

**Delivered:** `PlanStep`, `StepVerifier`, failure perturbation classes,
`PlanDAG`, progress views, eval scenarios, health snapshots, tracing, doctor
checks, and support bundles.

**Known correction:** later audit found that trusted acceptance facts and full
remaining-plan recovery are not yet enforced. M13 and M14 own those corrections.

### M6 - Long-task plan and context model

**Status:** `LANDED`

**Objective:** Represent long work without relying on retained chat turns.

**Delivered locally:** `ProgramPlan`, `ProgramPhase`, `LongTaskProjector`,
`LongTaskProjection`, `PendingInteraction`, `ArtifactRecord`,
`SideEffectRecord`, and `ContextPackBuilder`.

**Evidence:** focused tests plus the full 240-test local check.

### M7 - Long-task ledger boundary

**Status:** `LANDED`

**Objective:** Separate long-task interactions, artifacts, and side effects from
core task contracts, runs, events, and checkpoints.

**Delivered locally:** `LongTaskStore` and `InMemoryLongTaskStore`.

### M8 - SQLite long-task durability

**Status:** `LANDED`

**Objective:** Preserve long-task ledger records across local process restarts.

**Delivered locally:** `SqliteLongTaskStore` with tables for pending
interactions, task artifacts, and side-effect records.

### M9 - Parked-run classification

**Status:** `LANDED`

**Objective:** Expose which runs are paused, waiting for users, retryable, terminal,
or operator-owned without mutating them.

**Delivered locally:** `ParkedRunScanner` and scheduler-facing read models.

### M10 - DAG execution assessment

**Status:** `LANDED`

**Objective:** Identify a conservative executable frontier without scheduling it.

**Delivered locally:** `DagExecutionPolicy` and conflict/read-only safety reasons.

### M11 - Side-effect result ledger

**Status:** `LANDED`

**Objective:** Record externally visible results with run-scoped idempotency keys.

**Delivered locally:** runtime writes applied side-effect records on normal,
retry, alternative-tool, and approval paths.

**Known limitation:** the record is currently written after the external action.
It is an audit ledger, not yet a crash-safe outbox. M15 owns that correction.

### M12 - Long-task progress view

**Status:** `LANDED`

**Objective:** Give app/operator surfaces one read-only long-task snapshot.

**Delivered locally:** `LongTaskProgressReader` combining task progress, DAG
projection, parked state, and execution assessment.

## 7. Ordered Delivery Stages

### M13 - Acceptance trust and immutable task truth

**Status:** `LANDED`

**Objective:** Make it impossible for model output or later contract overwrites to
falsely complete or reinterpret an existing run.

**MVP scope:**

- split planner-proposed acceptance requirements from trusted runtime facts
- reject model-supplied evidence, passed tests, freshness, and human approvals
- require at least one required acceptance criterion
- make saved task contracts immutable or explicitly revisioned
- bind each run to the exact contract revision used at creation
- preserve behavior across in-memory, SQLite, and PostgreSQL adapters
- add regression tests for every discovered false-completion path

**Not in scope:** worker scheduling, full-plan continuation, API work, optional
capability migration, or concurrency.

**Entry gate:** M1-M12 behavior remains green and the current local M6-M12 work is
kept intact.

**Exit gate:**

- a zero-step model plan cannot self-assert tests or approval and complete
- an all-optional contract is rejected
- saving a different payload under an existing contract identity is rejected or
  creates a distinct revision
- an old run always resolves its original contract revision
- adapter contract tests pass for memory, SQLite, and PostgreSQL
- the full repository check passes

**Local evidence:** planner acceptance fields are rejected; `AgentPlan` no longer
contains runtime facts; contracts require a required criterion; all three stores
enforce immutable contract identity; 244 tests and the full local check pass.

### M14 - Persisted plans and complete recovery continuation

**Status:** `LANDED`

**Objective:** Resume the whole unfinished task, not only the last failed tool.

**MVP scope:**

- persist immutable `AgentPlan`/`ProgramPlan` revisions
- bind runs and checkpoints to a plan revision and current frontier
- reconstruct remaining work from events, checkpoints, and the persisted plan
- route retry, alternative-tool, and approval recovery back into one continuation
  path
- create and resolve durable pending interactions through runtime/app services
- inject `ContextPack` into continuation planning

**Not in scope:** background workers or parallel DAG execution.

**Exit gate:**

- in a three-step task that fails at step two, recovery executes step two and then
  step three exactly as required
- completed steps are not repeated unless policy explicitly marks them repeatable
- restart from SQLite reconstructs the same frontier without chat history
- all open interactions are anchored to checkpoints and resume tokens
- no recovery path bypasses step verification or final acceptance

**Local evidence:** immutable plans are stored by memory, SQLite, and PostgreSQL
adapters; run contract/plan bindings cannot change; retry, alternative-tool, and
approval recovery share the verified step path; a `ContextPack` drives the
remaining frontier; trusted acceptance facts survive restart as append-only
events; durable interactions resolve through approval; three-step, adversarial
replacement, and SQLite restart tests pass; 258 tests and the full local check
pass.

### M15 - Crash-safe side-effect outbox

**Status:** `LANDED`

**Objective:** Prevent duplicate or invisible external actions across crashes and
retries.

**MVP scope:** durable intent before execution, stable idempotency keys passed to
adapters, outbox state transitions, reconciliation, and explicit uncertain/manual
review state.

**Exit gate:** crash injection before dispatch, after dispatch, and before result
commit cannot silently duplicate a confirmed action; recovery can distinguish
planned, applied, confirmed, failed, reverted, and uncertain outcomes.

**Local evidence:** side-effect tools declare deterministic intents before
dispatch; the logical idempotency key is stable across retries; message adapters
receive that key; file writes reconcile identical content by hash; memory and
SQLite stores enforce compare-and-set state transitions; applied result snapshots
replay after restart; non-idempotent interrupted dispatch becomes `uncertain` and
requires evidence-backed reconciliation; fault injection covers intent persisted,
dispatch returned, and result commit boundaries; 270 tests and the full local
check pass.

### M16 - Durable worker and run ownership

**Status:** `LANDED`

**Objective:** Execute long tasks in the background with explicit ownership and
bounded recovery.

**MVP scope:** list/claim APIs, leases, heartbeat, lease expiry, retry budget,
backoff, dead-letter/manual-review state, cooperative pause/cancel, and a
PostgreSQL long-task ledger adapter.

**Not in scope:** multi-node parallel DAG execution.

**Exit gate:** only one live worker owns a run; expired ownership can be reclaimed;
restart preserves progress; retries stop at budget; waiting-user runs consume no
worker lease.

**Local evidence:** memory, SQLite, and PostgreSQL TaskStore adapters implement
exclusive claim, heartbeat, expiry reclaim, status compare-and-set, backoff, and
retry budgets; PostgreSQL now implements the long-task/outbox ledger; the worker
routes created, running, and recovering runs through `HarnessRuntime`; SQLite
restart preserves plans, progress, and lease state; waiting-user, paused, and
cancelled runs release ownership; cooperative cancellation stops before the next
step; dead-letter state is visible to the parked-run scanner; 290 tests and the
full local check pass.

### M17 - Real product execution MVP

**Status:** `LANDED`

**Objective:** Make the harness usable from a real local product entry point.

**MVP scope:** task submission CLI/API, explicit model and secret configuration,
tool-schema-aware planning, request context, structured status/result responses,
and one concrete gateway or local adapter.

**Exit gate:** a fresh checkout can configure a model, submit a task, inspect its
progress, answer an approval request, survive a restart, and receive a verified
result through documented commands.

**Local evidence:** `HarnessRuntime.submit` and `AgentOrchestrator.submit` persist
an immutable contract, plan, request context, and created run without tool
dispatch; the local JSON adapter exposes submit, status, work, approval, and
result reads; OpenAI-compatible configuration resolves secrets only from an
explicit environment variable; planner prompts contain host tool schemas and
reject unavailable tools or model-granted confirmation; model contract identity
is rebound to the host request; an SQLite end-to-end test closes and reopens the
application between submission, worker parking, resume-token approval, and a
verified file result; 299 tests and the full local check pass.

### M18 - Reliability, evaluation, and CI gates

**Status:** `LANDED`

**Objective:** Turn correctness, recovery, and latency expectations into repeatable
release evidence.

**MVP scope:** persistent benchmark corpus, short-plan and long-task suites,
failure perturbation tests, crash/restart tests, latency budgets, type/lint checks,
and CI.

**Exit gate:** CI blocks semantic regressions; benchmark results are versioned;
false completion, duplicate side effects, abandoned runs, and latency regressions
have explicit thresholds. Benchmarks remain observers, never completion authority.

**Local evidence:** versioned corpus `2026.07.1` executes six real lifecycle
cases across short plans, long-task retry continuation, outbox crash replay,
SQLite approval restart, and expired-lease reclaim; release thresholds require a
100% pass rate, zero false completions, zero duplicate side effects, zero
abandoned runs, per-case latency budgets, and a 15-second suite budget; the
machine-readable benchmark passes 6/6 in under 0.1 seconds locally; the unified
check runs 308 tests, benchmark, compileall, Ruff, mypy over 82 source files, CLI
smoke checks, and package dry-run; the workflow runs the same command on Python
3.11 and 3.13 from `.github/workflows/quality.yml` in the canonical root layout.

### M19 - Controlled optional capability migration

**Status:** `DEFERRED`

**Objective:** Migrate only capabilities required by the product after the core
MVP is reliable.

Each capability is a separate approved slice with its own boundary tests. The
candidate order is durable memory, skill lifecycle, MCP, cron/scheduled jobs, and
OpenGUI. The stage must never land them as one combined refactor.

Wiki, Graph-RAG, and geo are `REMOVED` from the current target. Reopening them
requires a new product decision and roadmap change.

### M20 - Safe DAG concurrency

**Status:** `DEFERRED`

**Objective:** Reduce latency without weakening task truth or side-effect safety.

**Entry gate:** M15, M16, and M18 are complete; execution ownership and fault
tests are proven sequentially.

**Initial scope:** only dependency-free, read-only, side-effect-free steps with
deterministic result merge and sequential fallback.

**Exit gate:** conflicting writes cannot run together; cancellation and failure
propagate deterministically; parallel and sequential runs produce equivalent
accepted outcomes for the same deterministic scenario.

### M21 - Migration closure and legacy deletion

**Status:** `LOCAL`

**Objective:** Make `re_zlagent` the only maintained ZLAgent implementation.

**Entry gate:**

- M13-M18 are complete
- every old capability is marked replaced, intentionally removed, or explicitly
  deferred with owner and reason
- required M19 slices are complete
- no source or test imports the old backend
- end-to-end and benchmark gates pass
- repository backup/tag and rollback instructions exist
- every dirty legacy path is reviewed and either preserved in an approved source
  snapshot or explicitly retained outside the deletion set
- credentials and runtime data are excluded from every source snapshot
- the user explicitly approves deletion after reviewing the final ledger

**Exit gate:** old source is removed in one focused change, verification passes
from a clean checkout, and rollback evidence is retained outside the deleted tree.

**Executed repository-root closure:**

1. The 38 dirty legacy entries were reviewed and saved in commit `54e47b2`; no
   `.env`, `workspace/`, or rebuilt source was included in that snapshot.
2. The complete validated rebuild was saved in commit `510436a`. Annotated tag
   `pre-rebuild-root-promotion` points to that commit and therefore retains both
   the reviewed legacy state and rebuilt tree.
3. The rebuilt package, tests, rules, README, license, ignore policy, and workflow
   were promoted from `re_zlagent/` to the repository root.
4. Tracked legacy product material was removed: `backend/`, `OpenGUI-main/`, old
   tests/config/scripts, Docker/start-stop wrappers, old runtime documentation,
   old requirements, old notices, and old media assets.
5. `.env`, `workspace/`, databases, logs, credentials, and other runtime data were
   not deleted or committed. Approximately 1.9 GB of ignored legacy build/runtime
   artifacts was moved intact to `.zlagent/legacy-runtime/`.
6. Root release-gate and clean-checkout evidence are required before M21 can move
   from `LOCAL` to `LANDED`.
7. Code rollback uses the annotated pre-promotion tag. Runtime data rollback stays
   outside Git and requires its own backup.

**Pre-closure repository footprint (2026-07-11):** Git tracked 2,338 files:
1,954 under `OpenGUI-main/`, 229 under `backend/`, 107 under `re_zlagent/`, 23
under `workspace/`, and 25 other root-level legacy product files. The candidate
legacy product change affected 2,208 tracked files. Outside `re_zlagent/`, the
worktree contained 31 modified files, 3 tracked deletions, and 4 untracked paths.
The approved closure preserved those entries in Git and reduced the active index
to 171 tracked files, including 23 intentionally retained `workspace/` files.

## 8. Capability Decisions

This is the M21 deletion ledger. `DEFERRED` means deletion intentionally removes
the old implementation and a future reintroduction must use current harness
boundaries; it does not mean the capability has already been migrated.

| Legacy capability area | Decision | Current replacement or reason | Owner/gate |
| --- | --- | --- | --- |
| task contract, events, checkpoints, acceptance | `REPLACED` | immutable plans, append-only truth, trusted acceptance | M13-M14 |
| long-task continuation and worker ownership | `REPLACED` | context packs, leases, retry budgets, dead-letter | M14-M16 |
| side-effect safety | `REPLACED` | deterministic intents and crash-safe outbox | M15 |
| local model planning and operator flow | `REPLACED` | schema-bounded JSON planner and local product CLI | M17 |
| SQLite/PostgreSQL task semantics | `REPLACED` | TaskStore and LongTaskStore adapters; live PostgreSQL DSN remains environment validation | M13-M16 |
| read/write file, URL read, message send, tool discovery | `REPLACED` | bounded tools, permissions, evidence, read-before-write | M2-M4 |
| confirmation and run controls | `REPLACED` | pending interactions, resume tokens, pause/resume/cancel/fork | M3-M5, M14 |
| observability, doctor, support bundle, progress | `REPLACED` | read-only facade, traces, health and progress snapshots | M6-M12 |
| release evaluation | `REPLACED` | versioned semantic/recovery/latency corpus and quality gate | M18 |
| concrete Weixin, WeCom and Webhook gateways | `DEFERRED` | only normalized gateway contracts and local CLI exist; deletion removes live IM entry points | product decision before deletion |
| FastAPI routes and deployment wrappers | `DEFERRED` | no HTTP product server is in the current MVP | product decision before deletion |
| durable memory and retrieval | `DEFERRED` | versioned in-memory boundary exists; durable provider/retrieval is not migrated | M19, user/product owner |
| skill curator, consolidation, review and usage lifecycle | `DEFERRED` | read-only loader and guard exist; mutation lifecycle is not migrated | M19, user/product owner |
| MCP install, transport and dynamic tool lifecycle | `DEFERRED` | requires a separate permission/credential/outbox slice | M19, user/product owner |
| cron and scheduled delivery | `DEFERRED` | requires durable scheduler ownership and delivery semantics | M19, user/product owner |
| OpenGUI and Android execution | `DEFERRED` | explicitly excluded from the core rebuild | M19, user/product owner |
| code execution, web search, delegation/subagents | `DEFERRED` | high-risk/product-specific tools need separate sandbox and acceptance slices | user/product owner |
| plugins, rich rendering, Redis cache and prompt cache | `DEFERRED` | not required by the local product MVP | user/product owner |
| wiki, Graph-RAG, geo and travel visited-map domain | `REMOVED` | explicitly removed from the current target | reopen roadmap to restore |
| default DAG concurrency | `DEFERRED` | sequential semantics are proven; concurrency requires a separate review | M20 |

**Deletion decision resolved:** on 2026-07-11 the user approved the local-core
closure, local snapshot commits and tag, root promotion, and tracked legacy source
deletion without pushing. Deferred capabilities are intentionally absent from the
active product until separately approved M19/M20 work restores them through the
current boundaries.

## 9. Definition of Done

A stage is complete only when all of the following are true:

1. Its scope and non-scope are still accurate.
2. Required code and migrations are implemented through the owning boundaries.
3. Focused tests cover success, rejection, failure, and recovery paths.
4. The full `re_zlagent.check` command passes.
5. No unrelated user changes were overwritten or reverted.
6. English authority docs are updated; paired Chinese user docs are synchronized.
7. Risks and intentionally deferred work are visible.
8. The stage status is evidence-based: uncommitted work remains `LOCAL`; only
   committed validated work becomes `LANDED`.

A task run is complete only when:

- every required step is passed or explicitly skipped by contract-backed policy
- every required acceptance criterion has trusted evidence
- required freshness windows hold
- required human approvals come from an authenticated interaction path
- no unresolved required interaction remains
- side effects are in a terminal, inspectable state
- `AcceptanceGate` returns accepted from those facts

## 10. Execution Discipline

- Work in stage order. Do not pull later capabilities into an earlier stage.
- Keep one engineering objective per change and one explicit owner boundary per
  module.
- A failed exit gate reopens the stage; it does not get explained away in prose.
- Correct state through new events/checkpoints or explicit revisions, never by
  erasing history.
- Update this English file first. Synchronize the Chinese translation in the same
  documentation change, but do not use the translation to drive AI decisions.
- Do not restore deleted legacy source wholesale after M21. Inspect the recovery
  tag only for a separately approved, bounded capability slice.
