# re_zlagent Repository Guide

## 1. Workspace Positioning

`re_zlagent/` is the new ZLAgent rebuild workspace. It is not the old backend and not an OpenGUI fork.

The parent repository contains reference material:

- `backend/` is old ZLAgent source. Read-only unless the user approves a migration slice.
- `tests/` is old test material. Read-only unless explicitly migrated.
- `OpenGUI-main/` is a local reference for repository structure and mobile-agent runtime ideas.
- `zlagent-governance-lab/` is historical draft material, not current authority.

This workspace follows OpenGUI's repository-rule style:

```text
AGENTS.md       thin entry pointer
CLAUDE.md       single source of truth for agents
README.md       human-facing project state and usage
CONTRIBUTING.md concise development workflow
src/            implementation
tests/          executable boundary checks
```

Do not reintroduce long-lived `docs/`, `plans/`, or `reports/` folders unless the user explicitly asks for them.

## 2. Default Working Rules

- Reply to the user in Chinese.
- Read `AGENTS.md`, then this file before working in `re_zlagent`.
- Treat everything outside `re_zlagent/` as reference-only by default.
- Keep each change focused on one explicit objective.
- Before new features, refactors, deletions, dependency changes, database changes, or batch edits, write a Chinese plan and wait for user confirmation.
- Simple reads, tests, code explanation, and obvious small fixes can be executed directly.
- Do not overwrite, revert, or delete user changes unless the user explicitly confirms the risk.
- Every code change must run appropriate validation. If a path cannot be verified, say so.

Required plan fields:

```text
本次目标
用户 / 干系人
MVP 范围
不做什么
预计文件结构或改动范围
验收标准
风险与取舍
```

Completion reports must state:

```text
改了什么
改了哪些文件
如何验证
发现的问题
剩余风险
可沉淀的新规则
```

## 3. Architecture Direction

The target is a DeerFlow-style agent harness adapted to ZLAgent's IM-first personal-assistant use case.

Target layers:

```text
gateway          inbound/outbound channels
app              API, IM adapters, operator surfaces
harness          reusable agent runtime, policies, tools, sandbox, tasking
facade           read-only inventory and runtime snapshots for app/gateway
agent            lead-agent orchestration and optional delegation
tasking          contracts, events, checkpoints, acceptance gates
tools            bounded capabilities with permission/evidence/error metadata
skills           reusable workflows
sandbox          filesystem/process boundary
memory           durable facts and notes
observability    traces, evals, doctor checks, support bundles
storage          persistence adapters and schema boundaries
progress         read-only polling snapshots from stored run state
```

Dependency direction:

```text
app -> harness
gateway -> app -> harness
harness -> tools / sandbox / tasking / memory / storage / observability
app/gateway -> HarnessFacade for inventory and status snapshots
```

Forbidden shortcuts:

- tools calling an agent loop directly
- storage depending on agent logic
- memory sending gateway messages directly
- harness importing app code
- direct writes without read-before-write or equivalent version checks
- side execution paths that bypass the normal run lifecycle
- claiming completion from model prose instead of acceptance evidence

Project structure discipline:

- New capabilities must first fit an existing layer before creating a new directory.
- Public imports for a module should be exposed from that module's `__init__.py`.
- Public Python imports must use the `re_zlagent.*` namespace; do not create top-level `app`, `gateway`, or `harness` packages.
- Runtime lifecycle controls belong in `src/re_zlagent/harness/runtime/` unless they become a cross-process transport concern.
- Do not create new top-level folders for plans, reports, experiments, or one-off notes unless the user explicitly asks.
- Tests should mirror the owning module boundary, not the implementation detail that happened to change.
- Every module should have one clear owner boundary: model, storage, runtime, tasking, tool, app, gateway, or observability.

## 4. Current Implementation

Current landed core:

```text
src/re_zlagent/gateway/
  DeliveryTarget
  IncomingMessage
  OutgoingMessage
  GatewayAdapter

src/re_zlagent/app/
  AgentApplication
  ApplicationDispatcher
  ApplicationResult
  DispatchResult
  ApplicationBootstrapConfig
  ApplicationContainer
  OperatorService
  OperatorResponse
  build_application_container
  run_cli

src/re_zlagent/harness/
  HarnessFacade
  HarnessInventory
  HarnessRuntimeSnapshot
  build_harness_facade

src/re_zlagent/harness/tools/
  Tool
  ToolResult
  ToolRegistry
  ToolSearchResult
  PermissionPolicy
  ReadBeforeWritePolicy

src/re_zlagent/harness/tools/builtins/
  ReadFileTool
  WriteFileTool
  SendMessageTool
  ReadUrlTool

src/re_zlagent/harness/sandbox/
  WorkspacePathPolicy

src/re_zlagent/harness/tasking/
  TaskContract
  TaskRun
  TaskEventLog
  Checkpoint
  CheckpointStore
  AcceptanceGate
  RecoveryPolicy
  ResumePolicy
  PlanDAG
  PlanStep
  StepStatus
  StepVerification
  StepVerifier

src/re_zlagent/harness/storage/
  TaskStore
  InMemoryTaskStore
  SqliteTaskStore
  PostgresTaskStore
  storage serde helpers

src/re_zlagent/harness/runtime/
  RunControlAction
  RunControlResult
  RunControlService
  RuntimeToolStep
  RuntimeAcceptanceInput
  RuntimeResult
  HarnessRuntime
  HarnessRuntime.resume_from_checkpoint
  HarnessRuntime.resume_with_user_approval

src/re_zlagent/harness/agent/
  AgentRunRequest
  AgentPlan
  AgentPlanner
  StaticAgentPlanner
  JsonPlanPlanner
  AgentOrchestrator

src/re_zlagent/harness/model/
  ModelMessage
  ModelResponse
  ModelClient
  OpenAICompatibleModelClient

src/re_zlagent/harness/memory/
  MemoryEntry
  MemoryKind
  MemorySource
  MemoryStore
  InMemoryMemoryStore
  MemoryManager

src/re_zlagent/harness/skills/
  SkillManifest
  FileSystemSkillLoader
  SkillGuard
  scan_skill_text

src/re_zlagent/harness/observability/
  TraceSpan
  TraceEvent
  TraceRecorder
  InMemoryTraceRecorder
  DoctorRunner
  DoctorReport
  build_harness_doctor
  SupportBundleBuilder
  redact_mapping

src/re_zlagent/harness/evals/
  EvalScenario
  EvalCaseResult
  EvalSuiteResult
  AgentEvalRunner
  RunHealthMonitor

src/re_zlagent/harness/progress/
  TaskProgressReader
  TaskProgressSnapshot
```

Important semantics:

- Tool output must include structured metadata: `status`, `error_type`, `recoverable_by_model`, `recommended_next_action`, `source`, `evidence`, `side_effects`.
- Tool discovery must stay read-only and expose capability boundaries without executing tools.
- Mutation tools that edit existing files require read-before-write.
- `SendMessageTool` is confirm-tier and records message side effects; it depends on a harness `MessageSender` protocol, not `gateway`.
- `ReadUrlTool` is safe/read-only and must return URL evidence with `fetched_at` metadata for freshness checks.
- `TaskEventLog` is append-only and idempotent by key.
- `Checkpoint` stores recoverable state and failure envelopes.
- `ResumePolicy` only allows automatic resume for explicitly recoverable retry checkpoints.
- `PlanStep` and `StepVerifier` define linear stage completion; they do not decide task completion.
- `PlanDAG` validates dependency shape and linear execution order only; it must not schedule parallel execution.
- Runtime emits `plan_step_started` and `plan_step_verified` events around tool execution.
- Stage verification failure stops the run before final acceptance and records `step_verification_failed`.
- Failure envelopes classify perturbations with `visibility`, `duration`, and `perturbation_class`.
- `resume_with_user_approval` resumes ask-user checkpoints, but approved tool success still requires step verification and final acceptance.
- `AcceptanceGate` can only pass from explicit evidence, tests, approvals, and freshness timestamps.
- `TaskStore` is the persistence boundary for task contracts, run projections, append-only events, and checkpoints.
- `InMemoryTaskStore` is the adapter behavior baseline used by tests.
- `SqliteTaskStore` is the local durable SQL behavior baseline.
- `PostgresTaskStore` preserves the same append-only semantics for production SQL.
- `HarnessRuntime` is the single lifecycle skeleton for deterministic runs before adding LLM planning.
- Runtime execution must flow through `TaskStore`, `ToolRegistry`, checkpoints, and `AcceptanceGate`.
- Operator controls such as pause, resume with feedback, cancel, and fork must go through `RunControlService`.
- Run controls mutate only the run projection and append lifecycle events/checkpoints; they must not execute tools or decide acceptance.
- `OperatorService` is the app-level reusable boundary for status, pause, resume, cancel, and fork.
- Operator surfaces must return structured data and must not infer task completion.
- Forked runs keep lineage in metadata and events instead of copying source event history.
- Runtime resume must be anchored to checkpoint events; retry creates new events and checkpoints instead of overwriting old failure state.
- Non-retry recovery actions such as ask-user, read-before-write, alternative-tool, and manual-review stop at a visible waiting/failure state.
- Resumed runs must pass `AcceptanceGate`; a successful retry is not completion by itself.
- `AgentPlanner` produces contracts and runtime steps; it must not execute tools directly.
- `JsonPlanPlanner` accepts strict JSON plans and validates them into `AgentPlan`.
- `OpenAICompatibleModelClient` is a thin provider adapter with injectable transport and no hard third-party dependency.
- Model-supplied freshness timestamps are rejected; freshness must come from trusted runtime tools.
- `AgentOrchestrator` is a thin bridge from planner output into `HarnessRuntime`.
- `GatewayAdapter` only sends normalized outbound messages.
- `AgentApplication` maps gateway messages into agent requests and formats results; it does not execute tools or decide acceptance.
- `ApplicationDispatcher` sends app output through a `GatewayAdapter`; delivery failures are returned as data.
- `build_application_container` assembles store, tools, runtime, planner, orchestrator, app, progress reader, and facade.
- `run_cli` is an app/operator surface. It must output JSON through `OperatorService`.
- App bootstrap requires an explicit planner or model; it must not silently pretend an LLM exists.
- `ApplicationContainer.close()` closes owned adapters that expose a `close` method.
- `MemoryStore` owns versioned durable memory entries.
- `MemoryManager` builds fenced memory context and strips fake memory-context tags from untrusted text.
- `FileSystemSkillLoader` reads Hermes `SKILL.md` and legacy `skill.yaml + instructions.md`.
- `SkillGuard` statically classifies safe, caution, and dangerous skill text.
- `TraceRecorder` records observability facts only; it must not decide runtime completion.
- Trace metadata is redacted before storage.
- `DoctorRunner` aggregates explicit diagnostics; failed checks become data, not crashes.
- `build_harness_doctor` checks facade-level readiness without mutating harness components.
- `SupportBundleBuilder` creates redacted issue summaries and diagnostics manifests.
- `AgentEvalRunner` runs benchmark scenarios against `AgentOrchestrator` output; it reports differences but never mutates task state.
- `RunHealthMonitor` builds read-only health snapshots from runtime results.
- Benchmarks and realtime health checks must not replace `AcceptanceGate`.
- `TaskProgressReader` builds polling snapshots from `TaskStore`; it is read-only and must not emit or mutate events.
- `HarnessFacade` is the app/gateway-facing read-only entry for capability inventory and runtime snapshots.
- App/gateway code should not reach directly into registries/loaders when a facade method covers the need.
- PostgreSQL must not replace append-only events with lossy overwrite-only task state.

Recommended future storage split:

```text
task_contracts       stable goal and acceptance contract
task_events          append-only lifecycle and tool/eval evidence
task_checkpoints     recoverable snapshots
task_runs            current projection only
```

If `task_runs` conflicts with events/checkpoints, recovery should trust events/checkpoints.

Current SQL storage baseline:

```text
SqliteTaskStore
  task_contracts(payload_json)
  task_runs(current projection + payload_json)
  task_events(append-only payload_json, unique run_id/seq, idempotency key)
  task_checkpoints(payload_json, unique run_id/seq)

PostgresTaskStore
  task_contracts(jsonb payload)
  task_runs(current projection + jsonb payload)
  task_events(append-only jsonb payload, unique run_id/seq, partial idempotency index)
  task_checkpoints(jsonb payload, unique run_id/seq)
  mutation paths lock the run projection with for update
```

PostgreSQL real-environment validation still requires an application-level DSN and a live database.

## 5. Migration Closure Plan

Migration closure is a staged process. Do not treat the old `backend/` as a file-copy target. Read old code to identify behavior, then preserve, replace, defer, or remove it deliberately.

Current closure status:

```text
re_zlagent harness foundation          implemented and tested
linear stage-completion model          implemented and tested
DAG expression model                   implemented and tested
failure perturbation classification    implemented and tested
user approval resume path              implemented and tested
app operator control surface           implemented and tested
old backend full capability parity     not complete
old backend deletion                   not allowed yet
OpenGUI-specific migration             deferred
```

Capability ledger:

| Old capability | Current `re_zlagent` status | Decision |
| --- | --- | --- |
| agent loop / tool loop | partially replaced | Keep the new `AgentPlanner -> HarnessRuntime -> AcceptanceGate` path instead of copying the old loop. |
| tool registry / permission | replaced | Use the new structured `ToolResult` and permission metadata. |
| checkpoints / recovery | replaced | Keep the new checkpoint and resume semantics. |
| task store / sqlite / postgres | replaced | Use `TaskStore` semantics as the source of truth. |
| app / gateway message boundary | foundation implemented | Use `AgentApplication` and `OperatorService` as app boundaries; add concrete IM/API adapters later. |
| memory | partially replaced | Keep minimal versioned memory now; old curator/review flows are deferred. |
| skills | partially replaced | Keep read-only loader and guard now; old `skill_manage` flows are deferred. |
| MCP | not migrated | Migrate as a separate approved stage. |
| cron / scheduled jobs | not migrated | Defer until the core harness is stable. |
| OpenGUI tool | not migrated | Defer while `re_zlagent` remains harness-first. |
| wiki / graph-rag / geo | not migrated | Defer as knowledge-system work. |
| FastAPI API layer | not migrated | Build after the app/harness boundary is stable. |
| confirmations | partially replaced | Confirm-tier semantics and user approval resume exist; concrete product confirmation adapters are deferred. |

Execution order:

```text
M1 migration ledger and scope freeze
M2 minimal stage-completion model implemented
M3 minimal replacement for core old-backend gaps implemented
M4 failure-classification and recovery hardening implemented
M5 DAG expression after linear stages are stable implemented
M6 safe concurrency after DAG boundaries are stable paused
M7 small regression/eval set after the runtime semantics settle
```

Stage rules:

- M2 implements a linear stage-completion model. Future work must preserve the distinction between step completion and task completion.
- M3 closes only minimal core old-backend gaps: read-only tool discovery and user approval resume. Product-specific integrations remain deferred.
- M4 uses ToolMaze-style failure classes after stage completion has explicit evidence.
- M5 expresses dependencies, but must not enable default parallel execution.
- M6 may parallelize only read-only, dependency-free, side-effect-free nodes.
- M7 measures reliability; it does not replace `AcceptanceGate`.
- Old source deletion requires an explicit final approval and a capability-ledger check.

## 6. Sensitive Areas

Treat these as high-risk:

- `src/re_zlagent/gateway/`: inbound/outbound message contracts and adapter boundary.
- `src/re_zlagent/app/`: app-to-harness wiring and user-facing result formatting.
- `src/re_zlagent/app/bootstrap.py`: application assembly, store choice, default tool registration, and planner/model boundary.
- `src/re_zlagent/app/operator.py`: operator-facing status and lifecycle control shape.
- `src/re_zlagent/harness/tools/`: permission, evidence, side effects, and recoverability contracts.
- `src/re_zlagent/harness/tools/builtins/file_tools.py`: filesystem boundary and read-before-write enforcement.
- `src/re_zlagent/harness/sandbox/`: path escape and workspace isolation.
- `src/re_zlagent/harness/tasking/`: completion contract, checkpoint, and recovery rules.
- `src/re_zlagent/harness/storage/`: task persistence semantics and adapter behavior baseline.
- `src/re_zlagent/harness/runtime/`: run lifecycle, control actions, event order, checkpoint anchoring, and acceptance transitions.
- `src/re_zlagent/harness/agent/`: planner boundary and orchestration path into runtime.
- `src/re_zlagent/harness/model/`: model provider boundary and strict response contracts.
- `src/re_zlagent/harness/memory/`: durable memory categories, versioned mutations, prompt-context fencing.
- `src/re_zlagent/harness/skills/`: read-only skill loading, path safety, duplicate detection, static safety scanning.
- `src/re_zlagent/harness/observability/`: trace spans, trace events, metadata redaction, diagnostics foundations.
- `src/re_zlagent/harness/evals/`: benchmark expectations and realtime health snapshots; no completion authority.
- `src/re_zlagent/harness/progress/`: read-only progress snapshots from task runs, events, and checkpoints.
- `src/re_zlagent/harness/facade.py`: read-only capability inventory and runtime component status.
- PostgreSQL adapter: schema, event append semantics, concurrent update policy.
- future `gateway/` and `app/`: user-facing side effects and IM/API entry behavior.

Protocol changes that cross layers require tests on both sides of the boundary.

## 7. Verification Commands

Default verification for current code:

```bash
python -m unittest discover -s re_zlagent/tests
python -m compileall re_zlagent/src re_zlagent/tests
PYTHONPATH=re_zlagent/src python -m re_zlagent.app.cli --help
find re_zlagent -maxdepth 5 -type f | sort
```

When touching tools, also verify:

- permission allow/confirm/deny paths
- read-only tool discovery and confirm-tool filtering
- evidence and side effect metadata
- recoverability metadata
- read-before-write success and failure paths
- unsafe path rejection
- externally visible message tools require confirmation
- harness tools do not import re_zlagent.app or re_zlagent.gateway modules
- URL tools return freshness evidence and recoverable network errors

When touching tasking, also verify:

- empty or duplicate task contracts are rejected
- event seq is monotonic per run
- idempotency key retry does not duplicate events
- checkpoint stores failure envelope and resume anchor
- plan-step verification distinguishes stage completion from task completion
- failure envelopes preserve visibility, duration, and perturbation class
- resume policy only auto-allows retry checkpoints
- non-retry recovery actions remain explicit and inspectable
- acceptance cannot pass without explicit evidence/test/approval/freshness
- optional criterion failure does not block completion
- tool failure maps to a concrete recovery action

When touching storage, also verify:

- contracts can be saved and loaded
- runs require an existing contract
- duplicate run ids are rejected
- event append updates run `event_seq`
- idempotency key retries do not duplicate events
- run projection updates preserve event history
- checkpoint creation updates the current run anchor
- checkpoint failure envelopes survive storage round-trip
- unknown run ids reject events and checkpoints
- durable SQL adapters survive process/reopen round-trip

When touching runtime, also verify:

- run creation appends lifecycle events
- every runtime step emits `plan_step_started` before tool execution
- every runtime step emits `plan_step_verified` after local verification
- verified step failure stops before final acceptance
- tool results are recorded as events with evidence and side effects
- successful runs create checkpoints and finish only after acceptance passes
- tool failures create failure checkpoints and terminal or recoverable status
- confirm-tier tools enter `waiting_user` without explicit approval
- acceptance failure enters `acceptance_failed`
- blocked acceptance enters `waiting_user`
- retryable failed checkpoints can resume through `resume_from_checkpoint`
- missing checkpoints reject resume
- non-retry checkpoints require user/manual handling rather than auto-running
- resumed tool success still goes through acceptance
- user-approved confirm resume still goes through step verification and acceptance
- DAG dependency metadata is validated without enabling parallel execution
- pause creates a paused checkpoint and a visible paused run projection
- resume records operator feedback and returns paused/user-blocked runs to running
- cancel creates a terminal cancelled checkpoint and rejects later resume
- fork creates a new run with source lineage without copying old event history
- run projection `event_seq` matches appended event history

When touching agent orchestration, also verify:

- planner receives the original request context
- planner output is validated before execution
- orchestrator calls `HarnessRuntime` instead of executing tools directly
- accepted and failed runtime results propagate to agent-level result
- duplicate runtime step ids are rejected

When touching model/planner code, also verify:

- invalid JSON is rejected
- missing required contract fields are rejected
- unknown criterion types are rejected
- duplicate runtime step ids are rejected
- model-supplied freshness timestamps are rejected
- provider adapters validate request config, preserve auth boundaries, and reject malformed provider responses

When touching memory, also verify:

- memory entries reject empty and oversized content
- durable memory mutations require observed versions
- stale versions are rejected
- archived entries are excluded from normal search
- pinned entries sort before non-pinned entries
- capacity compaction avoids pinned entries and control axioms
- prompt memory context is fenced
- fake memory-context tags from untrusted text are stripped

When touching skills, also verify:

- Hermes `SKILL.md` frontmatter is parsed and stripped from body
- legacy `skill.yaml + instructions.md` still loads
- duplicate skill ids are rejected
- symlink/path escapes are rejected
- dangerous skill text is detected
- disabled guard does not block test fixtures

When touching observability, also verify:

- spans have valid lifecycle transitions
- events cannot be appended after span end
- trace filtering preserves span order
- sensitive metadata keys are redacted recursively
- doctor check exceptions become error checks
- harness facade doctor reports missing required components as error data
- support bundles redact doctor, trace, and extra metadata
- tracing does not influence runtime acceptance

When touching evals, also verify:

- scenarios require explicit expectations
- benchmark results compare against runtime output only
- suite pass rate and score are deterministic
- realtime health snapshots classify completed, waiting_user, recoverable, and failed runs
- evals and health checks do not mutate runtime state or decide acceptance

When touching progress, also verify:

- missing runs return an explicit missing snapshot
- completed runs show completed steps, acceptance, latest event, and latest checkpoint
- completed steps come from `plan_step_verified=passed`, not raw tool success
- recoverable failures expose the failed step from checkpoint failure envelopes
- acceptance failures expose failed criteria from acceptance events
- progress readers do not write events, checkpoints, or run projections

When touching harness facade, also verify:

- tool inventory exposes permission, read/write, destructive, side effect, and interrupt boundaries
- skill inventory is serializable and sorted
- subsystem errors are returned as data instead of crashing app/gateway reads
- runtime snapshots expose component presence without mutating underlying services

When touching app/gateway, also verify:

- inbound messages validate required identity and text fields
- app bootstrap requires planner or model and rejects both at once
- app bootstrap exposes facade/runtime/store without bypassing harness boundaries
- app bootstrap tests close durable stores and should not emit resource warnings
- app constructs `AgentRunRequest` from normalized gateway messages
- app does not execute tools directly
- app does not decide acceptance outside runtime result
- app dispatcher sends through `GatewayAdapter` and reports delivery failures as data
- outgoing messages carry run status metadata
- operator service returns serializable status and run-control responses
- operator service keeps status reads non-mutating
- app CLI returns JSON for status, accepted control actions, rejected control actions, and argument errors

## 8. Reference Policy

When learning from old ZLAgent:

- read first
- identify behavior
- decide preserve/change/remove
- migrate only the approved slice

When learning from OpenGUI:

- reuse repository-rule structure and runtime lifecycle ideas
- do not copy product-specific logic blindly
- keep `AGENTS.md` thin and `CLAUDE.md` authoritative

When learning from DeerFlow:

- reuse engineering principles, not product code
- prefer harness/app separation, single run lifecycle, bounded tools, sandbox, memory, skills, sub-agents, doctor checks, support bundles, and boundary tests

## 9. What Future Agents Should Assume

- `CLAUDE.md` is the current rule source.
- `README.md` is the human-facing project status.
- Old material outside `re_zlagent/` is reference-only.
- The project is not production-ready until storage, app/gateway, runtime lifecycle, and device/tool integrations are implemented and verified.
