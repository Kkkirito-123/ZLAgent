# ZLAgent 中文说明

**语言：** [English](./README.md) | [简体中文](./README.zh-CN.md)

> 本文件仅供用户阅读。AI 工程执行以英文 `README.md`、`CLAUDE.md` 和 `ROADMAP.md` 为准。

当前仓库是正式的 ZLAgent 重构项目。Python package 继续使用 `re_zlagent`
namespace，但仓库本身已经提升到根目录。

项目已经交付可复用、可验证、可恢复并带发布门槛的 agent harness 和本地产品流程；
产品特定集成继续作为明确的路线图切片。

## 仓库规则

- `AGENTS.md` 是 agent 入口规则。
- `CLAUDE.md` 是 AI 工作规则和架构边界的英文权威来源。
- `ROADMAP.md` 是阶段和迁移状态的英文权威来源。
- `CONTRIBUTING.md` 是开发流程说明。
- 所有 `*.zh-CN.md` 都是用户阅读翻译，不参与 AI 工程决策。
- 不新增长期存在的 `docs/`、`plans/`、`reports/` 目录，除非用户明确要求。

## 当前目录结构

```text
.
├── AGENTS.md
├── AGENTS.zh-CN.md
├── CLAUDE.md
├── CLAUDE.zh-CN.md
├── CONTRIBUTING.md
├── CONTRIBUTING.zh-CN.md
├── pyproject.toml
├── README.md
├── README.zh-CN.md
├── ROADMAP.md
├── ROADMAP.zh-CN.md
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

## 已实现核心能力

### Gateway

- `DeliveryTarget`
- `IncomingMessage`
- `OutgoingMessage`
- `GatewayAdapter`

### App

- `AgentApplication`
- `ApplicationDispatcher`
- `ApplicationResult`
- `DispatchResult`
- `ApplicationBootstrapConfig`
- `ApplicationContainer`
- `ApplicationRuntimeContainer`
- `LocalTaskAdapter`
- `OperatorService`
- `ApprovalService`
- `build_application_container`
- `build_application_runtime`
- `run_cli`

### Harness Facade

- `HarnessFacade`
- `HarnessInventory`
- `HarnessRuntimeSnapshot`
- `build_harness_facade`

### Tools

- `Tool`
- `ToolResult`
- `ToolRegistry`
- `ToolSearchResult`
- `PermissionPolicy`
- `ReadBeforeWritePolicy`
- `ReadFileTool`
- `WriteFileTool`
- `SendMessageTool`
- `ReadUrlTool`

### Tasking

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
- `StepVerifier`
- `DagExecutionPolicy`
- `ProgramPlan`
- `LongTaskProjector`
- `PendingInteraction`
- `ArtifactRecord`
- `SideEffectRecord`

### Storage

- `TaskStore`
- `InMemoryTaskStore`
- `SqliteTaskStore`
- `PostgresTaskStore`
- `PostgresLongTaskStore`
- `LongTaskStore`
- `InMemoryLongTaskStore`
- `SqliteLongTaskStore`

### Runtime

- `HarnessRuntime`
- `HarnessRuntime.submit`
- `RuntimeToolStep`
- `RuntimeAcceptanceFacts`
- `RuntimeResult`
- `RuntimeSubmission`
- `RunControlService`
- `ContextPackBuilder`
- `ParkedRunScanner`
- `DurableWorker`
- `resume_from_checkpoint`
- `resume_with_alternative_tool`
- `resume_with_user_approval`

### Agent / Model / Memory / Skills

- `AgentPlanner`
- `StaticAgentPlanner`
- `JsonPlanPlanner`
- `AgentOrchestrator`
- `AgentOrchestrator.submit`
- `OpenAICompatibleModelClient`
- `OpenAICompatibleModelConfig`
- `MemoryManager`
- `FileSystemSkillLoader`
- `SkillGuard`

### Observability / Evals / Progress

- `TraceRecorder`
- `DoctorRunner`
- `SupportBundleBuilder`
- `AgentEvalRunner`
- `RunHealthMonitor`
- `BenchmarkCorpus`
- `ReleaseBenchmarkRunner`
- `ReleaseBenchmarkReport`
- `TaskProgressReader`
- `LongTaskProgressReader`

## 当前交付状态

核心重构已经替代活跃旧目录。完整阶段历史、延期能力和验收门槛统一记录在用户版
[`ROADMAP.zh-CN.md`](./ROADMAP.zh-CN.md)；AI 使用英文 [`ROADMAP.md`](./ROADMAP.md)。

```text
M1-M5   LANDED   已提交的基础和恢复边界
M6-M12  LANDED   长任务投影、ledger、存储、scanner 和进度
M13     LANDED   验收信任与不可变任务事实
M14     LANDED   持久化计划与完整恢复继续执行
M15     LANDED   崩溃安全 side-effect outbox
M16     LANDED   持久 worker 所有权和重试预算
M17     LANDED   真实任务提交与执行 MVP
M18     LANDED   可靠性和发布门槛
M19-M20 DEFERRED 可选迁移和安全 DAG 并发
M21     LOCAL    根目录提升与旧迁移关闭，等待最终 clean-checkout 证据
```

本地产品 MVP 已支持持久化提交、worker 执行、批准恢复，以及跨进程重启读取
已验证结果。M18 已增加版本化 6-case release corpus、语义和时延阈值、Ruff、
mypy 和统一机器可读质量命令。仓库根 `.github/workflows/quality.yml` 会在 Python
3.11 和 3.13 上运行同一门槛。

## 本地产品 CLI

配置 OpenAI-compatible planner。密钥不放入命令参数，也不会写入持久任务元数据：

```bash
mkdir -p .zlagent
export ZLAGENT_MODEL_BASE_URL="https://provider.example/v1"
export ZLAGENT_MODEL_NAME="planner-model"
export OPENAI_API_KEY="..."
```

提交任务。提交只持久化不可变 contract、plan、request context 和 `created` run，
不会执行工具：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  --workspace . \
  submit run-001 "读取 README.md 并生成可验证结果" \
  --context-json '{"channel":"local"}'
```

执行并查看持久任务：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite --workspace . work run-001

PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite status run-001
```

如果 `status.pending_interactions` 中存在批准请求，使用其中的 `resume_token`。
confirm-tier 工具不能被 planner JSON 预先授权：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite --workspace . \
  approve run-001 --resume-token RESUME_TOKEN --feedback "批准"
```

读取持久输出和验收事实，不会重新执行任务：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite result run-001
```

只有持久 run 为 `completed` 且最新验收决定为 accepted 时，
`result.result.verified` 才是 true。无密钥本地 provider 必须显式传入
`--no-api-key`。`work --until-idle` 可处理当前可领取任务，`--max-ticks`
负责限制前台循环次数。

## 关键设计规则

- 工具结果必须包含结构化元数据，不能只有自然语言。
- 工具发现必须是只读行为，不能执行工具。
- 变更类工具需要 read-before-write 或等价版本检查。
- 对外可见消息发送属于 confirm-tier side effect。
- 复杂任务完成必须通过 `AcceptanceGate`。
- 阶段完成不等于任务完成。
- `PlanDAG` 只表达依赖，不默认并发调度。
- `ProgramPlan` 负责把 DAG step 组织成长任务阶段。
- `PendingInteraction` 是用户/操作员等待点，必须绑定 checkpoint 和 resume token。
- `ContextPackBuilder` 从持久状态重建恢复上下文，聊天历史不是事实源。
- `ProgramPlan` revision 不可变，run 不能改绑 contract 或 plan。
- retry、alternative-tool 和 approval 恢复会继续所有未完成且可执行的验证 frontier。
- 替代工具只能完成原持久化步骤，不能修改其 ID、依赖或验证要求。
- 可信 runtime acceptance facts 作为 append-only event 持久化，可跨进程重启保留。
- `LongTaskStore` 存储 pending interaction、artifact、side effect，不替代 `TaskStore`。
- `ParkedRunScanner` 只做 parked run 分类，不执行工具、不修改状态。
- `DagExecutionPolicy` 只给出保守调度建议，不执行 DAG step。
- `LongTaskProgressReader` 只读合并任务进度、长任务投影、parked 状态和 DAG 执行评估。
- `HarnessRuntime` 是唯一工具执行骨架。
- `HarnessRuntime.submit` 只持久化计划并保持 run 为 `created`；工具执行只能进入 runtime/worker continuation。
- side-effect 工具必须在 dispatch 前声明确定性 intent，并使用 durable outbox。
- 逻辑 side-effect key 绑定 run、plan revision 和原 step，retry 必须复用同一下游幂等键。
- outbox 通过 compare-and-set 在 `planned`、`dispatching`、`applied`、`confirmed`、`failed`、`reverted`、`uncertain` 间转换。
- `uncertain` 不能自动完成；人工确认成功必须提供说明和可重放结果。
- `RunLease` 把 worker ownership、heartbeat、retry budget、backoff 与 `TaskRun.status` 分离。
- TaskStore adapter 对 run control 和 exclusive worker claim 使用 compare-and-set。
- `DurableWorker` 只负责调度；执行、验证、恢复和验收仍全部经过 `HarnessRuntime`。
- waiting-user、paused、cancelled、terminal run 不保留 active worker lease。
- 任务完成只能由 `AcceptanceGate` 判定，不能由模型文本声明。
- 模型只能规划 host 提供 schema 的工具，且 planner JSON 不能授予 confirm-tier 权限。
- 模型提出的 contract id 和 goal 在持久化前会重新绑定为 host 持有的 request identity。
- CLI 只从明确命名的环境变量读取模型密钥。

## 验证命令

先安装项目质量工具，再运行唯一发布门槛：

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=src python -m re_zlagent.check
```

单项诊断命令：

```bash
PYTHONPATH=src python -m re_zlagent.check --skip-package
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m re_zlagent.benchmark --pretty
ruff check src tests
mypy src/re_zlagent
python -m compileall src tests
PYTHONPATH=src python -m re_zlagent.app.cli --help
```

完整包检查：

```bash
PYTHONPATH=src python -m re_zlagent.check --pretty
```

## 下一步

核心迁移正在 M21 中关闭。后续产品工作必须先形成明确路线图决策：M19 负责独立可选
能力切片，M20 负责安全 DAG 并发。不得从恢复标签整体搬回任何 deferred 能力。

Wiki、Graph-RAG 和 geo 已从当前目标中移除。MCP、cron、OpenGUI 和 DAG 并发明确延后。

## 重要边界

仓库根是唯一活跃源码树。旧实现只能通过恢复标签为已批准切片提供行为参考，
`.zlagent/legacy-runtime/` 不得作为源码或规则输入。
