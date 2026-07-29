# ZLAgent Repository Guide

## 1. Workspace Positioning

This repository root is the canonical ZLAgent rebuild. It is not the old backend
and not an OpenGUI fork. The active Git tree contains only the rebuilt project,
plus explicitly retained `workspace/` material.

The pre-promotion implementation is recoverable from the annotated
`pre-rebuild-root-promotion` tag. Local generated artifacts may be retained under
`.zlagent/legacy-runtime/`; they are runtime data, not source or rule authority.

This workspace follows OpenGUI's repository-rule style:

```text
AGENTS.md       thin entry pointer
CLAUDE.md       single source of truth for agents
ROADMAP.md      delivery stages, status, and migration gates
README.md       human-facing project state and usage
CONTRIBUTING.md concise development workflow
src/            implementation
tests/          executable boundary checks
```

Do not reintroduce long-lived `docs/`, `plans/`, or `reports/` folders unless the user explicitly asks for them.

## 2. Default Working Rules

- Reply to the user in Chinese.
- Read `AGENTS.md`, then this file before working in this repository.
- Read `ROADMAP.md` before staged delivery, migration, or capability work.
- Use English authority files for AI decisions. Files ending in `.zh-CN.md` are
  human-facing translations; do not read them as operational context or authority.
- When an English authority document changes, synchronize its Chinese translation
  for the user in the same documentation change.
- Treat `.env`, `.zlagent/`, `workspace/logs/`, and `workspace/runtime/` as local
  data. Do not read, delete, move, stage, or commit them without explicit approval.
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
src/re_zlagent/check.py
  run_checks
  CheckReport

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
  ApplicationRuntimeContainer
  LocalTaskAdapter
  OperatorService
  OperatorResponse
  ApprovalService
  ApprovalResponse
  build_application_container
  build_application_runtime
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
  SchemaValidationIssue
  PermissionPolicy
  ReadBeforeWritePolicy
  validate_schema_definition
  validate_tool_arguments

src/re_zlagent/harness/tools/builtins/
  ReadFileTool
  WriteFileTool
  SendMessageTool
  ReadUrlTool
  InstallSkillTool

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
  DagExecutionAssessment
  DagExecutionPolicy
  ProgramPlan
  ProgramPhase
  LongTaskProjector
  LongTaskProjection
  PendingInteraction
  ArtifactRecord
  SideEffectRecord

src/re_zlagent/harness/storage/
  TaskStore
  InMemoryTaskStore
  LongTaskStore
  InMemoryLongTaskStore
  SqliteTaskStore
  SqliteLongTaskStore
  PostgresTaskStore
  PostgresLongTaskStore
  storage serde helpers

src/re_zlagent/harness/runtime/
  RunControlAction
  RunControlResult
  RunControlService
  RuntimeToolStep
  RuntimeAcceptanceFacts
  RuntimeResult
  RuntimeSubmission
  ContextPack
  ContextPackBuilder
  ParkedRunCandidate
  ParkedRunKind
  ParkedRunScanner
  DurableWorker
  HarnessRuntime
  HarnessRuntime.submit
  HarnessRuntime.resume_from_checkpoint
  HarnessRuntime.resume_with_alternative_tool
  HarnessRuntime.resume_with_user_approval

src/re_zlagent/harness/agent/
  AgentRunRequest
  AgentPlan
  AgentPlanner
  StaticAgentPlanner
  JsonPlanPlanner
  AgentOrchestrator
  AgentOrchestrator.submit

Current local M22 slice:

src/re_zlagent/harness/agent/
  GeneralAgent
  GeneralAgentMode
  GeneralAgentResult
  IntentDecision
  IntentRoute
  JsonIntentRouter

src/re_zlagent/harness/evals/
  IntentEvalCorpus
  IntentEvalRunner
  IntentEvalReport

src/re_zlagent/app/
  run_cli ask
  run_cli intent-eval

Current local M20/M23/M24 slice:

src/re_zlagent/harness/
  ContextManifest
  ContextManifestBuilder

src/re_zlagent/harness/agent/
  GeneralAgentMode.AUTO

src/re_zlagent/harness/memory/
  MemoryCaptureResult
  SqliteMemoryStore

src/re_zlagent/harness/runtime/
  RunBranchTree
  RunBranchTreeBuilder
  bounded read-only DAG batches

src/re_zlagent/app/
  run_cli branches

src/re_zlagent/harness/model/
  ModelCallBudget
  TokenBudgetExceededError
  complete_with_budget

src/re_zlagent/harness/evals/corpora/
  intent-routing-stress-v1.json

src/re_zlagent/app/
  run_cli intent-eval --stress

src/re_zlagent/harness/model/
  ModelMessage
  ModelResponse
  ModelClient
  OpenAICompatibleModelClient
  OpenAICompatibleModelConfig

src/re_zlagent/harness/memory/
  MemoryEntry
  MemoryKind
  MemorySource
  MemoryStore
  InMemoryMemoryStore
  MemoryManager

src/re_zlagent/harness/mcp/
  McpConfig
  McpServerConfig
  McpConfigurationError
  LocalMcpClient
  McpToolDescriptor
  McpProxyTool
  load_mcp_config
  create_mcp_tools

src/re_zlagent/harness/skills/
  SkillManifest
  FileSystemSkillLoader
  SkillGuard
  LocalSkillInstaller
  SkillInstallResult
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
  BenchmarkCorpus
  ReleaseBenchmarkRunner
  ReleaseBenchmarkReport

src/re_zlagent/harness/progress/
  TaskProgressReader
  TaskProgressSnapshot
  LongTaskProgressReader
  LongTaskProgressSnapshot
```

Important semantics:

- Tool output must include structured metadata: `status`, `error_type`, `recoverable_by_model`, `recommended_next_action`, `source`, `evidence`, `side_effects`.
- Tool schemas are runtime contracts, not prompt hints only. `ToolRegistry`
  rejects unsupported schema keywords at registration and validates arguments
  without coercion before permission or side-effect planning.
- Tool discovery must stay read-only and expose capability boundaries without executing tools.
- Mutation tools that edit existing files require read-before-write.
- `SendMessageTool` is confirm-tier and records message side effects; it depends on a harness `MessageSender` protocol, not `gateway`.
- `ReadUrlTool` is safe/read-only and must return URL evidence with `fetched_at` metadata for freshness checks.
- `TaskEventLog` is append-only and idempotent by key.
- `Checkpoint` stores recoverable state and failure envelopes.
- `ResumePolicy` only allows automatic resume for explicitly recoverable retry checkpoints.
- `PlanStep` and `StepVerifier` define linear stage completion; they do not decide task completion.
- `PlanDAG` validates dependency shape and linear execution order only; it must not schedule parallel execution.
- `ProgramPlan` groups DAG steps into long-task phases; it must not execute tools or schedule parallel work.
- `ProgramPlan` revisions are persisted before execution and are immutable by id.
- A run's contract and plan bindings are immutable; storage adapters must reject cross-contract plans and rebinding.
- `LongTaskProjector` derives step, phase, frontier, and blocked state from append-only events, checkpoints, and pending interactions.
- `PendingInteraction` is the durable wait point for user/operator input and must be anchored to a checkpoint plus resume token.
- `ContextPackBuilder` rebuilds resume context from `TaskStore`, artifacts, and pending interactions; chat history is not authoritative state.
- `LongTaskStore` persists pending interactions, artifact records, and side-effect records; it must not replace task contracts, events, runs, or checkpoints.
- `InMemoryLongTaskStore` is the adapter behavior baseline; `SqliteLongTaskStore` is the local durable SQL baseline for long-task ledger records.
- Side-effect tools must declare deterministic intents before dispatch, set `outbox_required`, and execute through `SideEffectOutbox`.
- Logical side-effect keys bind run id, immutable plan revision, and original step id; retry and recovery must reuse the same key downstream.
- `MessageSender` adapters must accept and deduplicate a stable idempotency key.
- Outbox transitions use compare-and-set across `planned`, `dispatching`, `applied`, `confirmed`, `failed`, `reverted`, and `uncertain`.
- A tool result event must be durable before `applied` becomes `confirmed`.
- `uncertain` outcomes require explicit reconciliation; confirmation must include a replayable trusted `ToolResult`.
- Undeclared side effects are quarantined as `uncertain` and cannot pass acceptance.
- `ParkedRunScanner` classifies paused, waiting-user, recoverable, terminal, and missing runs; it must not execute tools or mutate state.
- `RunLease` owns worker identity, token, heartbeat, expiry, attempt count, retry budget, backoff, and dead-letter state; these fields do not belong in `TaskRun.status`.
- TaskStore claim, heartbeat, release, and run-status transitions must be atomic compare-and-set operations in durable adapters.
- `DurableWorker` may schedule and heartbeat work, but must route created/running recovery and checkpoint retry through `HarnessRuntime`.
- `HarnessRuntime.submit` persists the immutable contract, plan, request context, and created run without executing tools.
- An active lease blocks competing workers; an expired lease may be reclaimed; waiting-user, paused, cancelled, and terminal runs cannot be claimed.
- Retry budget exhaustion produces durable dead-letter/manual-review state rather than an unbounded loop.
- `DagExecutionPolicy` gives conservative scheduling guidance for DAG frontier steps; it must not schedule or execute DAG steps.
- `LongTaskProgressReader` combines progress, long-task projection, parked state, and DAG execution assessment as read-only data.
- Long-task recovery context should preserve contract, DAG frontier, latest checkpoint, open interactions, artifact refs, evidence refs, recent events, and acceptance gaps.
- Retry, alternative-tool, and approval recovery must re-enter one continuation path driven by a persisted `ContextPack` and execute every remaining verified frontier step.
- Alternative tools execute as the original persisted step identity and cannot weaken its dependencies or verification requirements.
- Durable workers and product adapters must route execution through
  `HarnessRuntime`; automatic replan remains a separate later stage, while only
  M20-approved independent read-only DAG batches may overlap.
- Runtime emits `plan_step_started` and `plan_step_verified` events around tool execution.
- Stage verification failure stops the run before final acceptance and records `step_verification_failed`.
- Failure envelopes classify perturbations with `visibility`, `duration`, and `perturbation_class`.
- `resume_with_user_approval` resumes ask-user checkpoints, but approved tool success still requires step verification and final acceptance.
- `resume_with_alternative_tool` resumes only alternative-tool checkpoints and requires an explicit replacement `RuntimeToolStep`.
- `AcceptanceGate` can only pass from explicit evidence, tests, approvals, and freshness timestamps.
- `AgentPlan` contains requirements and runtime steps, never trusted acceptance facts.
- `RuntimeAcceptanceFacts` may only be constructed by trusted host/runtime or verifier boundaries; planner and model output cannot supply it.
- Trusted runtime acceptance facts are persisted as append-only events so restart does not change acceptance truth.
- Every task contract must contain at least one required acceptance criterion.
- `TaskStore.save_contract` is immutable by contract id: exact replay is idempotent and different content must use a new id.
- `TaskStore` is the persistence boundary for task contracts, run projections, append-only events, and checkpoints.
- `InMemoryTaskStore` is the adapter behavior baseline used by tests.
- `SqliteTaskStore` is the local durable SQL behavior baseline.
- `PostgresTaskStore` preserves the same append-only semantics for production SQL.
- `PostgresLongTaskStore` preserves pending interaction, artifact, and outbox ledger semantics for production SQL.
- `HarnessRuntime` is the single lifecycle skeleton for deterministic runs before adding LLM planning.
- Runtime execution must flow through `TaskStore`, `ToolRegistry`, checkpoints, and `AcceptanceGate`.
- Operator controls such as pause, resume with feedback, cancel, and fork must go through `RunControlService`.
- Run controls mutate only the run projection and append lifecycle events/checkpoints; they must not execute tools or decide acceptance.
- `OperatorService` is the app-level reusable boundary for status, pause, resume, cancel, and fork.
- Operator surfaces must return structured data and must not infer task completion.
- `ApprovalService` is the app-level reusable boundary for explicit user approval recovery.
- Approval surfaces must call `HarnessRuntime.resume_with_user_approval` and must not bypass step verification or `AcceptanceGate`.
- Forked runs keep lineage in metadata and events instead of copying source event history.
- Runtime resume must be anchored to checkpoint events; retry creates new events and checkpoints instead of overwriting old failure state.
- Non-retry recovery actions stop at a visible waiting/failure state unless an explicit recovery entry handles them.
- Resumed runs must pass `AcceptanceGate`; a successful retry is not completion by itself.
- `AgentPlanner` produces contracts and runtime steps; it must not execute tools directly.
- `JsonPlanPlanner` accepts strict JSON plans and validates them into `AgentPlan`.
- `JsonPlanPlanner` may make at most one default repair call after host validation
  rejects a plan. Repair attempts, plan length, and identical action repetition
  are bounded before a plan is persisted.
- Model planning is restricted to host-provided tool schemas; unavailable tools and model-granted confirm authority are rejected.
- Model-proposed contract identity and goal are rebound to the trusted host request before persistence.
- `OpenAICompatibleModelClient` is a thin provider adapter with injectable transport and no hard third-party dependency.
- `OpenAICompatibleModelConfig` stores only non-secret settings and resolves the API key from an explicitly named environment variable.
- Model-supplied freshness timestamps are rejected; freshness must come from trusted runtime tools.
- `AgentOrchestrator` is a thin bridge from planner output into `HarnessRuntime`.
- `GeneralAgent` is a presentation and embedding boundary above
  `AgentOrchestrator`; it never executes tools or changes acceptance truth.
- General-agent chat responses create no task run and are never marked verified.
- General-agent task responses may summarize bounded runtime output only after
  the existing runtime lifecycle has decided acceptance.
- CLI `ask` is an adapter over `GeneralAgent`; it must not introduce another
  planning, tool-execution, or acceptance path.
- `JsonIntentRouter` is read-only classification. It must not execute requests,
  select permissions, write memory, or assert task acceptance.
- Intent evaluation uses a versioned labeled corpus and reports accuracy,
  confusion, invalid output, latency, and usage without mutating task state.
- `auto` routing may resolve chat and clarification directly. A routed task may
  enter `HarnessRuntime` only when the host explicitly enables
  `allow_auto_task_execution`; the default remains disabled until real-model
  routing accuracy is measured.
- `GatewayAdapter` only sends normalized outbound messages.
- `AgentApplication` maps gateway messages into agent requests and formats results; it does not execute tools or decide acceptance.
- `ApplicationDispatcher` sends app output through a `GatewayAdapter`; delivery failures are returned as data.
- `build_application_container` assembles task, long-task, and memory stores,
  tools, runtime, planner, router, orchestrator, app, operator, approvals,
  progress reader, and facade.
- `build_application_runtime` assembles worker/operator services without requiring a planner or model after a plan has been persisted.
- `LocalTaskAdapter` exposes JSON-compatible submit, status, work, approval, and persisted result reads without creating a second execution path.
- A local result is verified only when persisted acceptance is true and the run projection is completed.
- `run_cli` is an app/operator surface. It must output JSON through `OperatorService`.
- App bootstrap requires an explicit planner or model; it must not silently pretend an LLM exists.
- `ApplicationContainer.close()` closes owned adapters that expose a `close` method.
- `MemoryStore` owns versioned durable memory entries.
- `SqliteMemoryStore` is the local durable memory adapter and may share the
  configured application SQLite file through its own connection.
- `MemoryManager` builds fenced relevant context, strips fake memory-context
  tags, and writes only from deterministic explicit remember-language.
- Model-inferred silent memory writes, embedding recall, and RAG remain
  forbidden until separately approved.
- `ContextManifest` records context source, trust, character budget,
  truncation, and token estimate; reader-facing metadata must omit segment
  content.
- `ModelCallBudget` owns per-call input, output, and total Token ceilings.
  Router and Planner over-budget failures must occur before Runtime execution.
- OpenAI-compatible adapters must enforce the lower of their global output
  ceiling and the phase ceiling; generic adapters still require preflight and
  post-call validation.
- Router and Planner may request provider JSON Output, but their content remains
  untrusted until the existing strict host parser validates it.
- CLI `ask` may expose normalized phase and aggregate Token usage, but must not
  expose provider response payloads, prompts, or credentials.
- Seed and stress intent corpora remain separate. Neither small corpus is
  sufficient evidence to enable automatic task execution by default.
- `RunBranchTreeBuilder` is a read-only projection over fork metadata; it must
  not copy events, checkpoints, plans, or mutable state between branches.
- Runtime concurrency may overlap only independent `SAFE` tools that are
  read-only, side-effect-free, outbox-free, and explicitly concurrency-safe.
  Confirmation, mutation, MCP, message, and Skill installation stay linear.
- `FileSystemSkillLoader` reads Hermes `SKILL.md` and legacy `skill.yaml + instructions.md`.
- `SkillGuard` statically classifies safe, caution, and dangerous skill text.
- `LocalSkillInstaller` accepts only bounded local packages, refuses overwrite
  conflicts, and treats byte-identical packages as idempotent replays.
- `InstallSkillTool` is confirm-tier and must execute through durable outbox
  intent; it never downloads, executes, updates, or deletes a Skill.
- `LocalMcpClient` owns approved local stdio subprocesses on a dedicated event
  loop, completes the official MCP lifecycle, and closes every session with the
  application container.
- MCP configuration must fail closed on unknown fields, unapproved commands,
  missing exact tool allowlists, missing named environment variables, unsupported
  transports, and tool schemas the Harness cannot enforce.
- MCP credentials are resolved only from named host environment variables. Their
  values must not enter config objects, errors, evidence, raw metadata, or source.
- Every MCP proxy is confirm-tier, retry-unsafe, and outbox-required regardless
  of remote annotations. Calls produce `mcp://server/tool` evidence and uncertain
  outcomes require manual review.
- `TraceRecorder` records observability facts only; it must not decide runtime completion.
- Trace metadata is redacted before storage.
- `DoctorRunner` aggregates explicit diagnostics; failed checks become data, not crashes.
- `build_harness_doctor` checks facade-level readiness without mutating harness components.
- `SupportBundleBuilder` creates redacted issue summaries and diagnostics manifests.
- `AgentEvalRunner` runs benchmark scenarios against `AgentOrchestrator` output; it reports differences but never mutates task state.
- `RunHealthMonitor` builds read-only health snapshots from runtime results.
- Benchmarks and realtime health checks must not replace `AcceptanceGate`.
- The release corpus is versioned package data; benchmark implementations execute real runtime/storage boundaries but remain read-only observers of completion truth.
- Release gates fail on any case regression, false completion, duplicate logical side effect, abandoned run, or latency budget violation.
- `re_zlagent.check` is the single local/CI quality command and includes tests, release benchmarks, compile, lint, type checks, smoke checks, and package validation.
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
pending_interactions durable user/operator wait points with resume tokens
task_artifacts       artifact refs and evidence lineage
side_effect_ledger   idempotency records for externally visible actions
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

## 5. Delivery Roadmap Authority

`ROADMAP.md` owns delivery stages, current status, capability decisions, entry
gates, exit gates, and the final legacy-deletion gate. Do not duplicate those
details in this file.

Migration rules that remain architectural authority here:

- Do not restore the tagged old `backend/` as a file-copy target. Inspect tagged
  behavior only for an approved capability slice, then preserve, replace, defer,
  or remove that behavior deliberately.
- Work in roadmap order and keep one engineering objective per approved change.
- A roadmap stage advances only from implementation and verification evidence.
  Model prose, status summaries, and intent are not evidence.
- Historical stages may be corrected without erasing their history.
- Wiki, Graph-RAG, and geo are excluded from the current target unless the
  roadmap is explicitly reopened.
- Reintroducing any deleted legacy capability requires a new roadmap decision and
  current boundary tests.

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
- `src/re_zlagent/harness/skills/`: read-only loading plus controlled local
  installation, path safety, duplicate/conflict detection, and static scanning.
- `src/re_zlagent/harness/observability/`: trace spans, trace events, metadata redaction, diagnostics foundations.
- `src/re_zlagent/harness/evals/`: benchmark expectations and realtime health snapshots; no completion authority.
- `src/re_zlagent/harness/progress/`: read-only progress snapshots from task runs, events, and checkpoints.
- `src/re_zlagent/harness/facade.py`: read-only capability inventory and runtime component status.
- PostgreSQL adapter: schema, event append semantics, concurrent update policy.
- future concrete gateway adapters: user-facing IM/API delivery and authentication behavior.

Protocol changes that cross layers require tests on both sides of the boundary.

## 7. Verification Commands

Default verification for current code:

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=src python -m re_zlagent.check
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m re_zlagent.benchmark --pretty
ruff check src tests
mypy src/re_zlagent
python -m compileall src tests
PYTHONPATH=src python -m re_zlagent.app.cli --help
find src tests -maxdepth 5 -type f | sort
```

When touching tools, also verify:

- unsupported tool schemas fail at registration instead of being ignored
- missing, mistyped, out-of-range, enum-invalid, and extra arguments are rejected
  before tool execution and side-effect planning
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
- alternative-tool resume requires explicit replacement step and still goes through step verification and acceptance
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

- schema-invalid tool arguments receive only the configured bounded repair calls
- repaired plans are fully revalidated and record their attempt count
- oversized plans and repeated identical tool actions are rejected before storage
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
- local installation requires confirmation and a durable filesystem intent
- package limits, manifest id mismatch, symlinks, and path escapes fail closed
- the candidate source must stay byte-stable across manifest validation
- byte-identical replay succeeds while different existing content is preserved
- a dispatch crash retries without creating a duplicate installed Skill

When touching MCP, also verify:

- only explicitly approved local stdio commands can start
- only exact allowlisted remote tools enter `ToolRegistry`
- missing named environment variables fail without exposing values
- SDK initialize, list, call, timeout, and close paths leave no subprocess leak
- unsupported remote schema keywords fail closed before planning
- every call waits for user approval and uses the normal Runtime/outbox path
- evidence uses `mcp://server/tool` provenance
- timeouts and unknown remote outcomes never retry automatically

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
- the versioned release corpus covers short plans, long continuation, crash replay, restart approval, and lease reclaim
- thresholds fail closed for false completion, duplicate side effects, abandoned runs, and latency regressions
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
- approval service returns serializable runtime recovery responses
- approval service still goes through step verification and acceptance after user approval
- app CLI returns JSON for status, accepted control actions, rejected control actions, and argument errors
- task submission remains `created` until a worker claims it, and no tool runs during submission
- planner prompts contain request context and registered tool schemas without model secrets
- planner output cannot select unavailable tools or set `allow_confirm=true`
- submit, work, approval, and verified result reads survive SQLite process reopen
- API keys are read from named environment variables and are absent from persisted plan/event metadata

When touching local verification, also verify:

- check command reports structured JSON
- check command injects `src` into `PYTHONPATH`
- check command can skip package metadata validation for quick local runs
- check command cleans generated Python cache and egg-info artifacts by default

## 8. Reference Policy

When learning from old ZLAgent through the recovery tag:

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
- `ROADMAP.md` is the stage and migration-status source.
- `README.md` is the concise human-facing project overview.
- `*.zh-CN.md` files are non-authoritative user translations. AI agents should
  not read them for working context or resolve conflicts from them.
- The repository root is the only active source tree. The recovery tag and local
  `.zlagent/legacy-runtime/` artifacts are not working-tree authority.
- The core local product is release-gated. Concrete IM gateways, HTTP deployment,
  durable retrieval memory, remote HTTP/OAuth MCP, MCP installation/update,
  MCP process sandboxing, deferred MCP schema loading, cron, OpenGUI, and DAG
  concurrency remain deferred product capabilities, not implied production
  support. Approved local stdio MCP transport and dynamic tools are implemented
  only through the bounded M19-MCP slice.
