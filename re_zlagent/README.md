# re_zlagent

`re_zlagent` is the new rebuild workspace for ZLAgent.

The goal is to rebuild ZLAgent as a reusable agent harness first, then add application and gateway layers later. The old source outside this directory is reference-only until a migration slice is explicitly approved.

## Repository Rules

- `AGENTS.md` is a thin entry pointer.
- `CLAUDE.md` is the source of truth for agent collaboration rules.
- `CONTRIBUTING.md` keeps the short development workflow.
- Avoid long-lived process-document folders unless the user explicitly asks for them.

## Current Structure

```text
re_zlagent/
├── AGENTS.md
├── CLAUDE.md
├── CONTRIBUTING.md
├── pyproject.toml
├── README.md
├── src/
│   └── re_zlagent/
│       ├── check.py
│       ├── app/
│       ├── gateway/
│       └── harness/
│           ├── facade.py
│           ├── agent/
│           ├── memory/
│           ├── model/
│           ├── observability/
│           ├── evals/
│           ├── progress/
│           ├── runtime/
│           ├── sandbox/
│           ├── skills/
│           ├── storage/
│           ├── tasking/
│           └── tools/
└── tests/
```

## Implemented Core

Gateway:

- `DeliveryTarget`
- `IncomingMessage`
- `OutgoingMessage`
- `GatewayAdapter`

App:

- `AgentApplication`
- `ApplicationDispatcher`
- `ApplicationResult`
- `DispatchResult`
- `ApplicationBootstrapConfig`
- `ApplicationContainer`
- `OperatorService`
- `OperatorResponse`
- `build_application_container`
- `run_cli`

Local check:

- `python -m re_zlagent.check`
- `re-zlagent-check`

Harness facade:

- `HarnessFacade`
- `HarnessInventory`
- `HarnessRuntimeSnapshot`
- `build_harness_facade`

Tool boundary:

- `Tool`
- `ToolResult`
- `ToolRegistry`
- `ToolSearchResult`
- `PermissionPolicy`
- `ReadBeforeWritePolicy`

Workspace file tools:

- `WorkspacePathPolicy`
- `ReadFileTool`
- `WriteFileTool`

Message tool:

- `SendMessageTool`
- `MessageSender`
- `ToolOutgoingMessage`

URL tool:

- `ReadUrlTool`
- `UrlFetcher`
- `UrlFetchResult`

Tasking:

- `TaskContract`
- `TaskRun`
- `TaskEventLog`
- `Checkpoint`
- `CheckpointStore`
- `AcceptanceGate`
- `RecoveryPolicy`
- `ResumePolicy`
- `PlanDAG`
- `PlanStep`
- `StepStatus`
- `StepVerification`
- `StepVerifier`

Storage:

- `TaskStore`
- `InMemoryTaskStore`
- `SqliteTaskStore`
- `PostgresTaskStore`

Runtime:

- `RunControlAction`
- `RunControlResult`
- `RunControlService`
- `RuntimeToolStep`
- `RuntimeAcceptanceInput`
- `RuntimeResult`
- `HarnessRuntime`
- `HarnessRuntime.resume_from_checkpoint`
- `HarnessRuntime.resume_with_user_approval`

Agent orchestration:

- `AgentRunRequest`
- `AgentPlan`
- `AgentPlanner`
- `StaticAgentPlanner`
- `JsonPlanPlanner`
- `AgentOrchestrator`

Model:

- `ModelMessage`
- `ModelResponse`
- `ModelClient`
- `OpenAICompatibleModelClient`

Memory:

- `MemoryEntry`
- `MemoryKind`
- `MemorySource`
- `MemoryStore`
- `InMemoryMemoryStore`
- `MemoryManager`

Skills:

- `SkillManifest`
- `FileSystemSkillLoader`
- `SkillGuard`
- `scan_skill_text`

Observability:

- `TraceSpan`
- `TraceEvent`
- `TraceRecorder`
- `InMemoryTraceRecorder`
- `DoctorRunner`
- `DoctorReport`
- `build_harness_doctor`
- `SupportBundleBuilder`
- `redact_mapping`

Evals:

- `EvalScenario`
- `EvalCaseResult`
- `EvalSuiteResult`
- `AgentEvalRunner`
- `RunHealthMonitor`

Progress:

- `TaskProgressReader`
- `TaskProgressSnapshot`

## Migration Closure Status

The rebuild is not a full replacement for the old `backend/` yet. Current status:

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

Closure stages:

```text
M1 migration ledger and scope freeze
M2 minimal stage-completion model implemented
M3 minimal replacement for core old-backend gaps implemented
M4 failure-classification and recovery hardening implemented
M5 DAG expression after linear stages are stable implemented
M6 safe concurrency after DAG boundaries are stable paused
M7 small regression/eval set after the runtime semantics settle
```

Do not delete old source until the capability ledger says every required old capability is replaced, deferred by explicit decision, or intentionally removed.

## Key Design Rules

- Tool results must carry structured metadata, not only natural language.
- Tool discovery is read-only and must expose tool boundaries without executing tools.
- Mutation tools require read-before-write or an equivalent version check.
- Externally visible message sends are confirm-tier side effects.
- URL reads return fetched-at evidence for freshness-sensitive acceptance.
- Complex task completion must pass `AcceptanceGate`.
- Stage completion is local: `PlanStep` and `StepVerifier` can pass a step, but they cannot complete a task.
- Runtime emits `plan_step_started` and `plan_step_verified` events for linear steps.
- `TaskProgressReader.completed_steps` is derived from verified passed steps, not raw tool success.
- `PlanDAG` validates dependency shape and linear order only; it does not schedule or parallelize.
- Failure envelopes classify perturbations by visibility and duration, such as `explicit_transient` and `implicit_permanent`.
- Recoverable retry checkpoints can resume through `HarnessRuntime.resume_from_checkpoint`.
- Ask-user checkpoints can resume through `HarnessRuntime.resume_with_user_approval`, then must pass step verification and acceptance again.
- Resume creates new events/checkpoints and preserves the original failure trace.
- Non-retry recovery actions stop for user input, read-before-write, alternative tooling, or manual review.
- A resumed run is not complete until `AcceptanceGate` passes again.
- Operator controls go through `RunControlService`, not ad hoc store updates.
- App/operator surfaces use `OperatorService` for status, pause, resume, cancel, and fork.
- Pause, resume, cancel, and fork are append-only lifecycle actions with visible events.
- Fork creates a new run with source lineage metadata; it does not copy old event history.
- Task history should be append-only; current run status is only a projection.
- `TaskStore` defines persistence semantics before any database adapter.
- `SqliteTaskStore` is the local durable SQL behavior baseline.
- `PostgresTaskStore` preserves append-only events and checkpoint semantics for PostgreSQL.
- `HarnessRuntime` is the single deterministic lifecycle path before LLM planning is added.
- `AgentPlanner` can produce a plan, but only `HarnessRuntime` executes tools and decides completion.
- LLM output must validate into `AgentPlan` before execution.
- Model provider adapters are thin transport boundaries; real keys and DSNs belong in app configuration.
- Freshness timestamps must come from trusted runtime evidence, not model JSON.
- Gateway/app are thin adapters around the harness, not alternate execution paths.
- Public imports use the `re_zlagent.*` namespace; do not add top-level `app`, `gateway`, or `harness` packages.
- Gateway delivery failures are dispatch data, not runtime acceptance decisions.
- App bootstrap assembles components; it requires an explicit planner or model.
- App CLI output is always JSON and uses `OperatorService` for run controls.
- App bootstrap containers should be closed when they own durable adapters.
- Durable memory mutations require observed versions.
- Memory context injected into prompts is fenced and sanitized.
- Skills are loaded read-only from bounded directories.
- Dangerous skill text is detected before future write paths are added.
- Trace metadata is redacted before storage.
- Doctor reports and support bundles are redacted by default.
- Observability records facts, not completion decisions.
- Runtime completion must pass `AcceptanceGate`, not model prose.
- Benchmark and realtime health checks observe runtime results; they do not replace acceptance.
- Progress snapshots are read-only polling views over task runs, events, and checkpoints.
- App/gateway should use the harness facade for inventory and runtime status snapshots.
- PostgreSQL storage is an adapter, not the owner of task semantics.

## Verification

Run:

```bash
PYTHONPATH=re_zlagent/src python -m re_zlagent.check --skip-package
python -m unittest discover -s re_zlagent/tests
python -m compileall re_zlagent/src re_zlagent/tests
PYTHONPATH=re_zlagent/src python -m re_zlagent.app.cli --help
find re_zlagent -maxdepth 5 -type f | sort
```

Current tests cover:

- gateway message models
- app application service
- app bootstrap container
- app operator service
- app JSON CLI
- app dispatcher
- tool result metadata
- tool discovery
- permission policy
- tool registry
- read-before-write policy
- workspace path policy
- file tools
- message tool
- URL tool
- task contracts
- plan steps and step verification
- static plan DAG validation
- task event log
- checkpoints
- acceptance gate
- recovery policy
- task store
- sqlite task store
- postgres task store
- runtime lifecycle
- runtime checkpoint resume behavior
- runtime run-control behavior
- agent orchestrator
- model JSON planner
- OpenAI-compatible model adapter
- memory store and prompt context
- skill loader and guard
- observability trace recorder
- doctor and support bundle
- harness doctor readiness check
- eval scenario runner and realtime health monitor
- task progress reader
- harness facade inventory and runtime snapshot
- local check command orchestration

## Not Implemented Yet

- live PostgreSQL DSN/config integration
- production model provider config and secrets management
- persistent benchmark corpus
- automatic replan or alternative-tool recovery
- product-level confirmation adapters and live IM/API approval flow
- OpenGUI integration
- MCP / cron / scheduled jobs migration
- wiki / graph-rag / geo knowledge systems
- safe concurrent DAG execution
- old backend deletion

## Package And CLI Smoke Checks

```bash
PYTHONPATH=re_zlagent/src python -m re_zlagent.app.cli --help
PYTHONPATH=re_zlagent/src python -m re_zlagent.check --skip-package
python -m pip install -e re_zlagent
zlagent --help
re-zlagent-check --skip-package
```
