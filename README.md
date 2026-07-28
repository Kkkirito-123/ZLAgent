# ZLAgent

**Language:** [English](./README.md) | [Simplified Chinese](./README.zh-CN.md)

This repository is the canonical rebuilt ZLAgent project. The Python package keeps
the `re_zlagent` namespace while the repository itself now lives at the root.

The project delivers a reusable, release-gated agent harness and local product
flow. Product-specific integrations remain explicit roadmap slices.

## Repository Rules

- `AGENTS.md` is a thin entry pointer.
- `CLAUDE.md` is the source of truth for agent collaboration rules.
- `ROADMAP.md` is the source of truth for delivery stages and migration status.
- `CONTRIBUTING.md` keeps the short development workflow.
- `*.zh-CN.md` files are human-facing translations. AI agents use the English
  authority files.
- Avoid long-lived process-document folders unless the user explicitly asks for them.

## Current Structure

```text
.
├── AGENTS.md
├── CLAUDE.md
├── CONTRIBUTING.md
├── pyproject.toml
├── README.md
├── ROADMAP.md
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
- `ApplicationRuntimeContainer`
- `LocalTaskAdapter`
- `OperatorService`
- `OperatorResponse`
- `ApprovalService`
- `ApprovalResponse`
- `build_application_container`
- `build_application_runtime`
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
- `SchemaValidationIssue`
- `PermissionPolicy`
- `ReadBeforeWritePolicy`
- `validate_schema_definition`
- `validate_tool_arguments`

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
- `DagExecutionAssessment`
- `DagExecutionPolicy`
- `ProgramPlan`
- `ProgramPhase`
- `LongTaskProjector`
- `LongTaskProjection`
- `PendingInteraction`
- `ArtifactRecord`
- `SideEffectRecord`

Storage:

- `TaskStore`
- `InMemoryTaskStore`
- `LongTaskStore`
- `InMemoryLongTaskStore`
- `SqliteTaskStore`
- `SqliteLongTaskStore`
- `PostgresTaskStore`
- `PostgresLongTaskStore`

Runtime:

- `RunControlAction`
- `RunControlResult`
- `RunControlService`
- `RuntimeToolStep`
- `RuntimeAcceptanceFacts`
- `RuntimeResult`
- `RuntimeSubmission`
- `ContextPack`
- `ContextPackBuilder`
- `ParkedRunCandidate`
- `ParkedRunKind`
- `ParkedRunScanner`
- `DurableWorker`
- `HarnessRuntime`
- `HarnessRuntime.submit`
- `HarnessRuntime.resume_from_checkpoint`
- `HarnessRuntime.resume_with_alternative_tool`
- `HarnessRuntime.resume_with_user_approval`

Agent orchestration:

- `AgentRunRequest`
- `AgentPlan`
- `AgentPlanner`
- `StaticAgentPlanner`
- `JsonPlanPlanner`
- `AgentOrchestrator`
- `AgentOrchestrator.submit`

`ToolRegistry` enforces the supported input-schema subset before permission and
side-effect planning. `JsonPlanPlanner` revalidates a complete plan after at
most one default repair call and rejects oversized or identical repeated actions
before persistence.

Model:

- `ModelMessage`
- `ModelResponse`
- `ModelClient`
- `OpenAICompatibleModelClient`
- `OpenAICompatibleModelConfig`

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
- `LocalSkillInstaller`
- `InstallSkillTool`
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
- `BenchmarkCorpus`
- `ReleaseBenchmarkRunner`
- `ReleaseBenchmarkReport`

Progress:

- `TaskProgressReader`
- `TaskProgressSnapshot`
- `LongTaskProgressReader`
- `LongTaskProgressSnapshot`

## Delivery Status

The core rebuild has replaced the active legacy tree. The authoritative stage
history, intentionally deferred capabilities, and acceptance gates are in
[`ROADMAP.md`](./ROADMAP.md).

```text
M1-M5   LANDED   committed foundation and recovery boundaries
M6-M12  LANDED   long-task projection, ledger, storage, scanner, and progress
M13     LANDED   acceptance trust and immutable task truth
M14     LANDED   persisted plans and complete recovery continuation
M15     LANDED   crash-safe side-effect outbox
M16     LANDED   durable worker ownership and retry budgets
M17     LANDED   real task submission and execution MVP
M18     LANDED   reliability and release gates
M19-SKILLS LANDED controlled local non-overwriting Skill installation
M19-M20 DEFERRED further optional migrations and safe DAG concurrency
M21     LANDED   root promotion and legacy closure verified from a clean checkout
```

The local product MVP supports persisted submission, worker execution, approval
recovery, and verified result reads across process restart. M18 adds a versioned
six-case release corpus, semantic and latency thresholds, Ruff, mypy, and one
machine-readable quality command. The repository-root workflow runs the same gate
on Python 3.11 and 3.13.

## Local Product CLI

Configure an OpenAI-compatible planner without placing a secret in command
arguments or persisted task metadata:

```bash
mkdir -p .zlagent
export ZLAGENT_MODEL_BASE_URL="https://provider.example/v1"
export ZLAGENT_MODEL_NAME="planner-model"
export OPENAI_API_KEY="..."
```

Submit a plan. Submission persists the immutable contract, plan, request context,
and a `created` run; it does not execute tools:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  --workspace . \
  submit run-001 "Read README.md and produce a verified result" \
  --context-json '{"channel":"local"}'
```

Execute and inspect durable work:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite --workspace . work run-001

PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite status run-001
```

When `status.pending_interactions` contains an approval request, use its
`resume_token`. Confirm-tier tools cannot be pre-approved by planner JSON:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite --workspace . \
  approve run-001 --resume-token RESUME_TOKEN --feedback "approved"
```

Controlled local Skill installation is opt-in. Create separate import and
managed roots, put one Hermes `SKILL.md` or legacy package under the import root,
and pass both roots to every `submit`, `work`, or `approve` process that may plan
or execute `install_skill`:

```bash
mkdir -p .zlagent/skill-imports .zlagent/skills
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  --skill-import-dir .zlagent/skill-imports \
  --skills-dir .zlagent/skills \
  submit run-skill-001 "Install the local demo Skill"
```

`source_path` is always relative to the configured import root. Installation
requires explicit approval, blocks symlinks/path escapes/dangerous text, and
never overwrites different content. Network download, Skill execution, update,
delete, dependency installation, and MCP are outside this slice.

Read persisted outputs and acceptance truth without re-executing the task:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite result run-001
```

`result.result.verified` is true only when the stored run is `completed` and the
latest persisted acceptance decision is accepted. For an unauthenticated local
provider, pass `--no-api-key` explicitly. `work --until-idle` processes bounded
claimable work, with `--max-ticks` preventing an unbounded foreground loop.

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
- `ProgramPlan` groups DAG steps into long-task phases; it does not execute them.
- `LongTaskProjector` derives step, phase, frontier, and blocked state from append-only events, checkpoints, and pending interactions.
- `PendingInteraction` is the durable wait point for user/operator input and carries a resume token.
- `ContextPackBuilder` rebuilds resume context from stored state; chat history is not the source of truth.
- `ProgramPlan` revisions are immutable, and a run cannot change its contract or plan binding.
- Retry, alternative-tool, and approval recovery continue every unfinished verified frontier step.
- Alternative tools satisfy the original persisted step; they cannot replace its identity, dependencies, or verification requirements.
- Trusted runtime acceptance facts are append-only events and survive process restart.
- `LongTaskStore` persists pending interactions, artifact records, and side-effect records; it does not replace `TaskStore`.
- `InMemoryLongTaskStore` is the behavior baseline; `SqliteLongTaskStore` is the local durable SQL baseline.
- `HarnessRuntime` writes tool side effects into `LongTaskStore` when one is configured; side-effect ledger writes must be idempotent by run and key.
- Side-effect tools must declare deterministic intents before dispatch and require a durable outbox.
- Logical side-effect keys bind run, plan revision, and original step, so retries reuse the same downstream idempotency key.
- Outbox states are compare-and-set transitions across `planned`, `dispatching`, `applied`, `confirmed`, `failed`, `reverted`, and `uncertain`.
- `uncertain` outcomes cannot auto-complete; reconciliation requires an explicit note and a replayable result when confirming success.
- `RunLease` keeps worker ownership, heartbeat, retry budget, and backoff separate from `TaskRun.status`.
- TaskStore adapters use compare-and-set for run control and exclusive worker claims.
- `DurableWorker` only schedules; all execution, verification, recovery, and acceptance remain in `HarnessRuntime`.
- `HarnessRuntime.submit` persists a plan and leaves the run `created`; only runtime/worker continuation executes tools.
- Waiting-user, paused, cancelled, and terminal runs do not retain an active worker lease.
- `ParkedRunScanner` classifies parked runs; it must not execute tools or mutate state.
- `DagExecutionPolicy` gives conservative scheduling guidance; it must not schedule or execute DAG steps.
- `LongTaskProgressReader` combines task progress, long-task projection, parked state, and DAG execution assessment as a read-only snapshot.
- Long-task resume context includes contract, DAG frontier, checkpoint, open interactions, artifacts, evidence refs, recent events, and acceptance gaps.
- Dependency-blocked steps are not executable frontier steps.
- Failure envelopes classify perturbations by visibility and duration, such as `explicit_transient` and `implicit_permanent`.
- Recoverable retry checkpoints can resume through `HarnessRuntime.resume_from_checkpoint`.
- Ask-user checkpoints can resume through `HarnessRuntime.resume_with_user_approval`, then must pass step verification and acceptance again.
- Alternative-tool checkpoints can resume only through an explicit alternative `RuntimeToolStep`.
- Resume creates new events/checkpoints and preserves the original failure trace.
- Non-retry recovery actions stop for user input, read-before-write, alternative tooling, or manual review unless an explicit recovery entry handles them.
- A resumed run is not complete until `AcceptanceGate` passes again.
- Operator controls go through `RunControlService`, not ad hoc store updates.
- App/operator surfaces use `OperatorService` for status, pause, resume, cancel, and fork.
- App approval surfaces use `ApprovalService` and must still pass `AcceptanceGate` after approval.
- Pause, resume, cancel, and fork are append-only lifecycle actions with visible events.
- Fork creates a new run with source lineage metadata; it does not copy old event history.
- Task history should be append-only; current run status is only a projection.
- `TaskStore` defines persistence semantics before any database adapter.
- `SqliteTaskStore` is the local durable SQL behavior baseline.
- `PostgresTaskStore` preserves append-only events and checkpoint semantics for PostgreSQL.
- `HarnessRuntime` is the single deterministic lifecycle path before LLM planning is added.
- `AgentPlanner` can produce a plan, but only `HarnessRuntime` executes tools and decides completion.
- Model-planned tools are restricted to host-provided schemas, and model JSON cannot grant confirm-tier authority.
- Model-planned contract ids and goals are rebound to host-owned request identity before persistence.
- LLM output must validate into `AgentPlan` before execution.
- Model provider adapters are thin transport boundaries; CLI keys are resolved only from an explicitly named environment variable.
- Freshness timestamps must come from trusted runtime evidence, not model JSON.
- Gateway/app are thin adapters around the harness, not alternate execution paths.
- Public imports use the `re_zlagent.*` namespace; do not add top-level `app`, `gateway`, or `harness` packages.
- Gateway delivery failures are dispatch data, not runtime acceptance decisions.
- App bootstrap assembles components; it requires an explicit planner or model.
- App CLI output is always JSON and uses `OperatorService` for run controls.
- App bootstrap containers should be closed when they own durable adapters.
- Durable memory mutations require observed versions.
- Memory context injected into prompts is fenced and sanitized.
- Skill inventory is loaded read-only from a bounded managed directory.
- The only Skill mutation path is a confirm-tier controlled local install through
  deterministic outbox intent; dangerous packages and overwrite conflicts fail
  closed, while identical content is an idempotent replay.
- Trace metadata is redacted before storage.
- Doctor reports and support bundles are redacted by default.
- Observability records facts, not completion decisions.
- Runtime completion must pass `AcceptanceGate`, not model prose.
- Benchmark and realtime health checks observe runtime results; they do not replace acceptance.
- Progress snapshots are read-only polling views over task runs, events, and checkpoints.
- App/gateway should use the harness facade for inventory and runtime status snapshots.
- PostgreSQL storage is an adapter, not the owner of task semantics.

## Verification

Install the project quality tools, then run the single release gate:

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=src python -m re_zlagent.check
```

Individual commands remain available for diagnosis:

```bash
PYTHONPATH=src python -m re_zlagent.check --skip-package
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m re_zlagent.benchmark --pretty
ruff check src tests
mypy src/re_zlagent
python -m compileall src tests
PYTHONPATH=src python -m re_zlagent.app.cli --help
find src tests -maxdepth 5 -type f | sort
```

Current tests cover:

- gateway message models
- app application service
- app bootstrap container
- app operator service
- app approval service
- app JSON CLI
- persisted submit/work/approve/result restart flow
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
- long-task ledger store
- sqlite long-task ledger store
- parked-run recovery scanner
- DAG safe-execution policy
- runtime side-effect ledger integration
- runtime lifecycle
- runtime checkpoint resume behavior
- runtime run-control behavior
- agent orchestrator
- model JSON planner
- OpenAI-compatible model adapter
- memory store and prompt context
- skill loader, guard, controlled install, and outbox replay
- observability trace recorder
- doctor and support bundle
- harness doctor readiness check
- eval scenario runner and realtime health monitor
- versioned release benchmark corpus and blocking threshold report
- false-completion, crash replay, restart approval, and expired-lease scenarios
- task progress reader
- long-task projection and pending interaction state
- context pack builder for long-task resume
- context pack loading from long-task ledger store
- long-task progress reader
- harness facade inventory and runtime snapshot
- local check command orchestration

## Next Work

The core migration is closed. Future product work must begin with an explicit
roadmap decision: M19 owns bounded optional capability slices and M20 owns safe
DAG concurrency. No deferred capability should be restored wholesale from the
legacy tag.

M19-SKILLS now provides only controlled local non-overwriting installation.
Wiki, Graph-RAG, and geo are removed from the current target. MCP, Skill
download/update/delete/execution, cron, OpenGUI, and DAG concurrency remain
explicitly deferred.

## Package And CLI Smoke Checks

```bash
PYTHONPATH=src python -m re_zlagent.app.cli --help
PYTHONPATH=src python -m re_zlagent.check --skip-package
python -m pip install -e .
zlagent --help
re-zlagent-check --skip-package
```
