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
│           ├── mcp/
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
- `GeneralAgent`
- `GeneralAgentMode`
- `GeneralAgentResult`
- `IntentDecision`
- `IntentRoute`
- `JsonIntentRouter`

`ToolRegistry` enforces the supported input-schema subset before permission and
side-effect planning. `JsonPlanPlanner` revalidates a complete plan after at
most one default repair call and rejects oversized or identical repeated actions
before persistence.

Model:

- `ModelMessage`
- `ModelResponse`
- `ModelClient`
- `ModelCallBudget`
- `TokenBudgetExceededError`
- `OpenAICompatibleModelClient`
- `OpenAICompatibleModelConfig`

Memory:

- `MemoryEntry`
- `MemoryKind`
- `MemorySource`
- `MemoryStore`
- `InMemoryMemoryStore`
- `SqliteMemoryStore`
- `MemoryManager`
- `MemoryCaptureResult`

Skills:

- `SkillManifest`
- `FileSystemSkillLoader`
- `SkillGuard`
- `LocalSkillInstaller`
- `GitHubSkillInstaller`
- `SkillSelector`
- `InstallSkillTool`
- `InstallGitHubSkillTool`
- `scan_skill_text`

MCP:

- `McpConfig`
- `McpServerConfig`
- `LocalMcpClient`
- `McpToolDescriptor`
- `McpProxyTool`
- `create_mcp_tools`
- `load_mcp_config`

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
- `IntentEvalCorpus`
- `IntentEvalRunner`
- `IntentEvalReport`
- `EffectivenessCorpus`
- `EffectivenessBenchmarkRunner`
- `EffectivenessReport`
- `ProjectEffectivenessExecutors`

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
M19-SKILLS-GITHUB LOCAL opt-in pinned GitHub Skill installation and selection
M19-MCP LANDED approved local stdio MCP lifecycle and dynamic tool adapters
M19     DEFERRED remaining optional capability migrations
M20     LOCAL    bounded read-only DAG concurrency
M21     LANDED   root promotion and legacy closure verified from a clean checkout
M22     LOCAL    general single-agent facade and measurable intent routing
M23     LOCAL    context manifest, explicit memory, and branch views
M24     LOCAL    token-bounded model I/O and mixed-request routing stress
M25     LOCAL    lightweight named sessions and rolling context compaction
```

The local product MVP supports persisted submission, worker execution, approval
recovery, and verified result reads across process restart. The release corpus
now contains 12 cases, including multi-retry continuation, persisted DAG-frontier
restart, alternative-tool recovery, mid-plan approval, retry-budget dead-letter
handling, and cooperative cancellation. It reports evidence-backed task
completion, long-task completion, and recovery rates in addition to the original
semantic and latency gates. The repository-root workflow runs the same gate on
Python 3.11 and 3.13.

## General Single-Agent Facade

`GeneralAgent` is the lightweight embedding surface for CLI, IM, IDE, or service
hosts. It keeps one facade with three request modes:

- `chat` makes one ordinary model response, creates no task run, executes no
  tools, and always reports `verified=False`.
- `task` preserves the existing planner, runtime, checkpoint, side-effect, and
  acceptance lifecycle. After trusted acceptance, an optional response model
  turns bounded tool outputs into a user-facing answer. This presentation call
  cannot change task truth.
- `auto` calls the strict intent router and resolves `chat`, `task`, or
  `clarify`. Chat and clarification are automatic. Routed task execution stays
  disabled by default and requires explicit host opt-in.

`build_application_container(model=...)` exposes the facade as
`container.general_agent`. A host with its own planner can pass a separate
`response_model`. Every request also produces a content-free
`ContextManifest` describing source, trust, character budget, truncation, and
estimated tokens. Explicit "remember ..." language is captured
deterministically into the configured memory store; model-inferred silent
memory, embeddings, RAG, and multi-agent delegation remain out of scope.
An optional `session_id` adds lightweight multi-turn continuity: 32 raw turns
remain available locally, up to 12 recent complete turns may enter the bounded
window, and the oldest prefix is compacted to an append-only rolling summary
that targets eight recent turns. Conversation context is low-trust presentation
data and never replaces checkpoints or `ContextPack` for durable tasks.
When a managed Skill inventory is configured, `SkillSelector` first scores only
manifest metadata, loads at most two matching instruction bodies, rejects a
dangerous match, and records the bounded selection in that same manifest. An
unmatched Skill contributes no instruction text or prompt tokens; selected
instruction bodies are also excluded from the intent-routing call:

```python
from re_zlagent.harness.agent import AgentRunRequest, GeneralAgentMode

result = await container.general_agent.run(
    AgentRunRequest(
        run_id="host-request-001",
        user_goal="Summarize this request",
        context={"surface": "ide"},
    ),
    mode=GeneralAgentMode.CHAT,
)
print(result.response, result.verified)
```

Conservative auto routing without automatic task execution:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  ask "Explain the checkpoint design" --mode auto
```

After reviewing router accuracy, a host may explicitly allow an auto-routed
task to enter the same permission and acceptance path. The default remains
gated:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite --workspace . \
  ask "Read README.md" --mode auto --auto-execute-task
```

The local CLI exposes the same facade. Chat does not require SQLite:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  ask "Explain what this Agent can do" --mode chat
```

A named CLI conversation uses SQLite so a later process can reopen the same
context:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  ask "I am planning a trip to Shanghai" --mode chat \
  --session-id personal-chat
```

Interactive task mode may persist its run and use configured workspace tools:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite --workspace . \
  ask "Read README.md and summarize the implemented core" \
  --mode task --run-id ask-readme-001
```

Both commands return compact JSON. `ok` means the request was handled; only
`verified` means a task completed through trusted runtime acceptance. Full
events, checkpoints, and tool payloads remain available through `status` and
`result` instead of being copied into the assistant response.

## Intent Routing Accuracy

`JsonIntentRouter` is a read-only structured router for `chat`, `task`, and
`clarify`. It calls no tools. `auto` mode uses it, but the default task gate
prevents a routed task from executing. Model output is accepted only when it
matches the strict route, reason-code, and clarification schema.

The shipped seed corpus contains 24 balanced Chinese/English cases: eight per
route. It is intentionally small and measures regression on clear examples,
not production accuracy. Run it against the configured model:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  intent-eval --model "router-model"
```

The JSON report includes overall accuracy, per-route and per-language accuracy,
invalid-output count, confusion matrix, average latency, and provider token
usage when available. The seed target is 85%. This command requires an actual
model configuration but does not require SQLite.

On 2026-07-29, `deepseek-v4-flash` classified the packaged corpus correctly in
24/24 cases: 100% for both languages and all three routes, with zero invalid
outputs, 1,829 ms average latency, and 8,753 total tokens. This small clear-case
corpus proves the integration works; it does not justify enabling automatic
task execution by default.

The separate mixed-request corpus covers negation, read-then-answer requests,
missing pronoun targets, and read-only external actions:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli intent-eval --stress
```

With provider JSON Output and the Router budget enabled,
`deepseek-v4-flash` classified that corpus correctly in 24/24 cases, with zero
invalid outputs, 1,985 ms average latency, and 9,180 total tokens. Seed and
stress reports remain separate so the clearer seed set cannot hide ambiguous
failures.

## Chinese Project Effectiveness Benchmark

The resume-evidence benchmark is separate from the blocking release corpus. Its
manifest is JSON, while cases use JSONL so one Chinese scenario can be reviewed,
extended, or diagnosed without rewriting a large JSON document:

```text
effectiveness-v1.manifest.json   version, language, tracks, capability matrix
effectiveness-v1.cases.jsonl     one independent Chinese scenario per line
runner output                    JSON, one-result-per-line JSONL, or Markdown
```

The packaged pilot contains 70 Chinese cases and 105 repeated observations across
intent routing, Harness reliability, memory/context, DAG/Token behavior, and 12
real-model task executions. Every case declares stable capability IDs, and the
manifest rejects a corpus with any uncovered required capability. `pilot` means
the schema and executors are usable, not that the numbers are ready for a resume.
Validate the frozen shape and 28-capability matrix without calling a model:

```bash
PYTHONPATH=src python -m re_zlagent.effectiveness_benchmark --validate-only --pretty
```

Run the 66 deterministic observations through real Runtime, storage, memory,
context, DAG, and model-budget boundaries:

```bash
PYTHONPATH=src python -m re_zlagent.effectiveness_benchmark \
  --deterministic-only --output-format markdown
```

Running all tracks requires the same real model configuration as `intent-eval`.
The intent adapter only observes `JsonIntentRouter`; it never executes tools. The
`agent_task` track asks the real planner to construct 1-6 step Chinese plans, then
executes only a safe benchmark evidence tool through `HarnessRuntime`. Completion
requires a completed run, all expected evidence, an acceptance contract covering
that evidence, correct ordering/dependencies, and zero false completion. Reports
separate benchmark pass rate from task and long-task completion rates and include
total, average, P95, and maximum planner Token usage. Reports may be emitted as
`json`, `jsonl`, or `markdown`. Before using results in a resume, freeze the corpus
version and run without changing cases after seeing failures.

On 2026-07-30, `deepseek-v4-flash` passed the 12-case `agent_task` pilot in
12/12 cases: task completion 12/12, long-task completion 10/10, zero false
completions, and zero planner repairs. Planner usage was 21,164 total tokens,
1,763.7 average, and 2,442 P95/maximum. This is one controlled pilot run, not a
production success-rate claim. The same model passed the 27 repeated intent
observations in 27/27 with 13,238 tokens (490.3 average). Together with 66/66
local deterministic observations, the independently executed tracks cover all
105/105 observations; provider-backed tracks used 34,402 tokens in total.

## Model Token Budgets

Every model phase has a simple hard boundary:

- Router: 2,048 input / 512 output tokens
- Planner: 16,000 input / 4,096 output tokens
- chat response: 8,192 input / 2,048 output tokens
- accepted-task response synthesis: 8,192 input / 1,024 output tokens

Input is conservatively estimated before a call. Compatible providers receive
the phase output cap, while `ZLAGENT_MODEL_MAX_TOKENS` may impose a lower global
ceiling. Provider usage is normalized into `token_usage.phases` and
`token_usage.aggregate` in CLI `ask` output. A Router or Planner preflight
failure occurs before Runtime execution; response-synthesis budget failure
falls back to already accepted Runtime output.

## Memory, Branches, and Safe DAG Batches

- SQLite applications persist versioned personal memory in the same database;
  recall remains bounded lexical search and does not require RAG.
- Only explicit remember-language writes memory. Repeated equivalent text is
  idempotent, and every write records its capture policy and source.
- `branches RUN_ID` projects a read-only tree from existing fork metadata. It
  copies no events, checkpoints, plans, or task truth.
- `HarnessRuntime` overlaps at most four ready steps only when their registered
  tools are independent, `SAFE`, read-only, side-effect-free, outbox-free, and
  explicitly concurrency-safe. Mutation and confirmation steps remain linear.

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite branches run-001
```

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
managed roots, put one Agent Skills `SKILL.md` or legacy package under the import root,
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
never overwrites different content.

GitHub installation is a separate host opt-in over the same managed root:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  --skills-dir .zlagent/skills \
  --allow-github-skill-install \
  submit run-skill-remote-001 \
  "Install demo from owner/repo@main#skills/demo"
```

`install_github_skill` accepts only `https://github.com/...` or
`owner/repo@ref#subpath`, requires confirmation, resolves the ref to a full
commit SHA, validates a standard `SKILL.md`, extracts a bounded regular-file
tree, reuses the non-overwriting local installer, and records provenance in
`skills.lock.json`. An outbox replay uses the locked commit and digest without a
second download. `GITHUB_TOKEN` is optional for API rate limits and is never
written to evidence. Arbitrary registries, automatic update/delete, dependency
installation, and Skill script execution remain outside the slice. MCP is
delivered separately below.

Local stdio MCP is also opt-in. Install the optional dependency and save a
host-owned JSON configuration outside source control, for example at
`.zlagent/mcp.json`:

```bash
python -m pip install -e '.[mcp]'
export GITHUB_TOKEN="..."
```

```json
{
  "servers": [
    {
      "id": "github",
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "your_mcp_server"],
      "approved": true,
      "tools": ["search_repositories", "get_file_contents"],
      "env": {"GITHUB_TOKEN": "GITHUB_TOKEN"},
      "startup_timeout_seconds": 10,
      "request_timeout_seconds": 30
    }
  ]
}
```

`approved: true` records approval of the exact local process command. `tools` is
an exact remote-name allowlist; unlisted tools never enter the Harness registry.
Each `env` value is the name of a host environment variable, never the secret
itself. Do not place credentials in `args` or commit the configuration.
The subprocess still runs with the current OS user's privileges: stdio and
approval reduce exposure but are not a process sandbox. Run only reviewed MCP
servers and scope their credentials narrowly.

Pass the same config to every process that plans or executes MCP tools:

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  --mcp-config .zlagent/mcp.json \
  submit run-mcp-001 "Use the approved MCP tool"

PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  --mcp-config .zlagent/mcp.json \
  work run-mcp-001
```

Remote names become bounded local names such as `mcp__github__search_repositories`.
All MCP calls are confirm-tier, declare a durable `mcp` outbox intent, and emit
`mcp://server/tool` evidence. Because a generic MCP server cannot guarantee
idempotency, a timeout or uncertain outcome requires manual review and is never
automatically retried. This slice intentionally excludes HTTP/OAuth transports,
server installation/update, MCP resources/prompts, and deferred schema loading.
OS-level process sandboxing also remains a separate hardening slice.

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
- `ContextManifest` makes context source and budget observable without echoing private segment content.
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
- `HarnessRuntime` may execute one bounded batch of independent read-only concurrency-safe frontier steps; all other frontier work is linear.
- `RunBranchTreeBuilder` projects fork lineage read-only and rejects missing or cyclic parent chains.
- Memory writes require deterministic explicit remember-language; no model output can silently persist memory.
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
- Skill inventory is loaded read-only from a bounded managed directory; only
  metadata-matched bodies enter a request Context Manifest.
- Skill mutation paths are confirm-tier controlled local or explicitly enabled
  GitHub installs through deterministic outbox intent. Dangerous packages,
  unsafe archives, and overwrite conflicts fail closed; GitHub provenance is
  pinned in `skills.lock.json` and Skill package code is never executed.
- MCP servers are host-configured local stdio processes with exact command
  approval, exact tool allowlists, named environment references, bounded
  schemas, and explicit lifecycle cleanup.
- MCP tool calls remain confirm-tier and retry-unsafe, execute only through the
  normal Runtime/outbox path, and produce `mcp://server/tool` evidence.
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
- MCP config validation, stdio lifecycle, allowlisting, approval, timeout,
  outbox, evidence, and secret-redaction boundaries
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

The core migration is closed. M20/M23/M24/M25 now provide bounded read-only
concurrency, conservative auto routing, explicit durable memory, context
manifests, lightweight named sessions, branch views, per-phase Token budgets,
and seed/stress routing reports. Automatic task execution remains host-enabled rather than default-on;
the next evidence gate is repeated and expanded evaluation from representative
host traffic, not another architecture layer.

Remote HTTP/OAuth MCP, server installation/update, resources/prompts, deferred
schema loading, arbitrary Skill registries, Skill update/delete/execution, cron, OpenGUI, RAG,
implicit memory mining, and multi-agent delegation remain explicitly deferred.
No deferred capability should be restored wholesale from the legacy tag.

## Package And CLI Smoke Checks

```bash
PYTHONPATH=src python -m re_zlagent.app.cli --help
PYTHONPATH=src python -m re_zlagent.check --skip-package
python -m pip install -e .
zlagent --help
re-zlagent-check --skip-package
```
