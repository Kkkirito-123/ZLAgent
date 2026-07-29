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
- `SchemaValidationIssue`
- `PermissionPolicy`
- `ReadBeforeWritePolicy`
- `validate_schema_definition`
- `validate_tool_arguments`
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
- `GeneralAgent`
- `GeneralAgentMode`
- `GeneralAgentResult`
- `IntentDecision`
- `IntentRoute`
- `JsonIntentRouter`

`ToolRegistry` 会在权限判断和副作用规划前强制执行受支持的输入 schema 子集。
`JsonPlanPlanner` 默认最多进行一次受控修复，并重新校验完整计划；超长计划和完全
相同的重复工具动作会在持久化前失败。
- `OpenAICompatibleModelClient`
- `OpenAICompatibleModelConfig`
- `ModelCallBudget`
- `TokenBudgetExceededError`
- `MemoryManager`
- `FileSystemSkillLoader`
- `SkillGuard`
- `LocalSkillInstaller`
- `InstallSkillTool`

MCP：

- `McpConfig`
- `McpServerConfig`
- `LocalMcpClient`
- `McpToolDescriptor`
- `McpProxyTool`
- `create_mcp_tools`
- `load_mcp_config`

### Observability / Evals / Progress

- `TraceRecorder`
- `DoctorRunner`
- `SupportBundleBuilder`
- `AgentEvalRunner`
- `RunHealthMonitor`
- `BenchmarkCorpus`
- `ReleaseBenchmarkRunner`
- `ReleaseBenchmarkReport`
- `IntentEvalCorpus`
- `IntentEvalRunner`
- `IntentEvalReport`
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
M19-SKILLS LANDED 受控本地且不覆盖的 Skill 安装
M19-MCP LANDED 已批准的本地 stdio MCP 生命周期和动态工具适配
M19     DEFERRED 其余可选能力迁移
M20     LOCAL    受限只读 DAG 并发
M21     LANDED   根目录提升与旧迁移关闭已通过全新 checkout 验证
M22     LOCAL    通用单 Agent 门面和可测量意图路由
M23     LOCAL    Context Manifest、显式记忆和分支视图
M24     LOCAL    Token 受限模型 I/O 和混合请求路由压力测试
```

本地产品 MVP 已支持持久化提交、worker 执行、批准恢复，以及跨进程重启读取
已验证结果。M18 已增加版本化 6-case release corpus、语义和时延阈值、Ruff、
mypy 和统一机器可读质量命令。仓库根 `.github/workflows/quality.yml` 会在 Python
3.11 和 3.13 上运行同一门槛。

## 通用单 Agent 门面

`GeneralAgent` 是供 CLI、IM、IDE 或服务 host 嵌入的轻量入口，保留一个门面和
三种请求模式：

- `chat` 只进行一次普通模型回答，不创建任务 run、不执行工具，并始终返回
  `verified=False`。
- `task` 继续使用现有 planner、runtime、checkpoint、副作用和 acceptance 生命周期。
  只有可信验收通过后，可选 response model 才会把受限工具结果整理为用户答复；
  这次展示层调用不能改变任务事实。
- `auto` 使用严格 Intent Router 解析为 `chat`、`task` 或 `clarify`。chat 和
  clarify 可自动处理；路由出的 task 默认仍被 gate，必须由 host 显式开启执行。

`build_application_container(model=...)` 会通过 `container.general_agent` 暴露该入口。
自带 planner 的 host 可以单独传入 `response_model`。每个请求还会产生不含正文的
`ContextManifest`，说明上下文来源、信任级别、字符预算、截断和 Token 估算。
显式“记住……”语言会通过确定性规则写入配置的 MemoryStore；模型静默推断记忆、
embedding、RAG 和多 Agent 委派仍不在范围内：

```python
from re_zlagent.harness.agent import AgentRunRequest, GeneralAgentMode

result = await container.general_agent.run(
    AgentRunRequest(
        run_id="host-request-001",
        user_goal="总结这个请求",
        context={"surface": "ide"},
    ),
    mode=GeneralAgentMode.CHAT,
)
print(result.response, result.verified)
```

保守自动路由，但不自动执行 task：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  ask "解释 checkpoint 设计" --mode auto
```

查看路由准确率后，host 可以显式允许自动 task 进入同一权限和验收路径；默认仍保持
gate：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite --workspace . \
  ask "读取 README.md" --mode auto --auto-execute-task
```

本地 CLI 暴露同一个门面。chat 不要求 SQLite：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  ask "说明这个 Agent 能做什么" --mode chat
```

交互式 task 模式可以持久化 run，并使用已配置的 workspace 工具：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite --workspace . \
  ask "读取 README.md 并总结已经实现的核心" \
  --mode task --run-id ask-readme-001
```

两个命令都返回紧凑 JSON。`ok` 只表示请求已被处理；只有 `verified` 表示任务通过
可信 Runtime 验收完成。完整 event、checkpoint 和工具 payload 仍通过 `status`
与 `result` 查看，不会复制进助手答复。

## 意图路由准确率

`JsonIntentRouter` 是只读的 `chat`、`task`、`clarify` 结构化 Router，不调用
工具。`auto` 模式已经使用它，但默认 task gate 会阻止路由出的 task 执行。只有
严格满足 route、reason code 和澄清问题 Schema 的模型输出才会被接受。

随包种子集包含 24 条中英文平衡样例，每类 8 条。它规模很小，只用于衡量清晰样例
上的回归，不代表生产准确率。使用已配置模型运行：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  intent-eval --model "router-model"
```

JSON 报告包含整体准确率、按 route 和语言的准确率、非法输出数、混淆矩阵、平均
时延，以及 provider 提供时的 Token usage。种子目标为 85%。该命令需要真实模型
配置，但不需要 SQLite。

2026-07-29 使用 `deepseek-v4-flash` 对随包语料实测：24/24 全部正确，中英文与
三个 route 均为 100%，非法输出为 0，平均时延 1,829 ms，总 Token 8,753。这个
小型清晰样例集证明集成链路可用，但不足以支持默认开启自动 task 执行。

独立的混合请求语料覆盖否定、读取后回答、代词目标缺失和只读外部动作：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli intent-eval --stress
```

启用 provider JSON Output 和 Router 预算后，`deepseek-v4-flash` 对该语料实测
24/24 全部正确，非法输出为 0，平均时延 1,985 ms，总 Token 9,180。seed 与 stress
报告保持分离，避免清晰样例掩盖歧义请求失败。

## 模型 Token 预算

每个模型阶段都有简单硬边界：

- Router：输入 2,048 / 输出 512 Token
- Planner：输入 16,000 / 输出 4,096 Token
- chat 回答：输入 8,192 / 输出 2,048 Token
- 已验收任务回答合成：输入 8,192 / 输出 1,024 Token

调用前会保守估算输入；兼容 provider 会收到阶段输出上限，
`ZLAGENT_MODEL_MAX_TOKENS` 还可以施加更低的全局上限。provider usage 会规范化到
CLI `ask` 输出的 `token_usage.phases` 和 `token_usage.aggregate`。Router 或 Planner
预检失败发生在 Runtime 执行前；回答合成超预算则回退到已验收 Runtime 输出。

## 记忆、分支与安全 DAG 批次

- SQLite 应用会把版本化个人记忆持久化到同一数据库；召回仍是受限关键词检索，
  不需要 RAG。
- 只有显式“记住”语言会写入记忆；重复等价内容具备幂等性，每次写入都记录捕获
  policy 和 source。
- `branches RUN_ID` 从已有 fork metadata 投影只读树，不复制 event、checkpoint、
  plan 或任务事实。
- `HarnessRuntime` 最多同时执行四个 ready 步骤；只有注册工具彼此独立，同时满足
  `SAFE`、只读、无副作用、不使用 outbox 且显式 concurrency-safe 才能并发。
  mutation 和 confirmation 步骤保持线性。

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite branches run-001
```

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

受控本地 Skill 安装需要显式启用。创建彼此独立的导入目录和托管目录，把一个
Hermes `SKILL.md` 或 legacy 包放入导入目录，并在可能规划或执行 `install_skill`
的每个 `submit`、`work`、`approve` 进程中同时传入两个目录：

```bash
mkdir -p .zlagent/skill-imports .zlagent/skills
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  --skill-import-dir .zlagent/skill-imports \
  --skills-dir .zlagent/skills \
  submit run-skill-001 "安装本地 demo Skill"
```

`source_path` 始终相对于配置好的导入目录。安装必须显式批准，会阻断符号链接、
路径逃逸和危险文本，也绝不覆盖不同内容。网络下载、Skill 执行、更新、删除、
依赖安装不属于 Skill 切片；MCP 作为下面的独立切片交付。

本地 stdio MCP 同样需要显式启用。先安装可选依赖，并把 host 持有的 JSON 配置
保存在源码管理之外，例如 `.zlagent/mcp.json`：

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

`approved: true` 表示 host 已确认精确的本地进程命令。`tools` 是远端工具名的
精确白名单，未列出的工具不会进入 Harness registry。`env` 的 value 只能是 host
环境变量名，不能写密钥值；不得把 credential 放进 `args`，也不要提交此配置。
subprocess 仍拥有当前 OS 用户的权限：stdio 和批准流程可以缩小暴露面，但不等于进程
sandbox。只能运行审查过的 MCP server，并给 credential 设置最小权限。

可能规划或执行 MCP 工具的每个进程都必须传入相同配置：

```bash
PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  --mcp-config .zlagent/mcp.json \
  submit run-mcp-001 "使用已批准的 MCP 工具"

PYTHONPATH=src python -m re_zlagent.app.cli \
  --sqlite .zlagent/tasks.sqlite \
  --mcp-config .zlagent/mcp.json \
  work run-mcp-001
```

远端名称会转换为 `mcp__github__search_repositories` 这类受限本地名称。所有 MCP
调用都是 confirm-tier，必须声明 durable `mcp` outbox intent，并产生
`mcp://server/tool` evidence。通用 MCP server 无法保证幂等，因此超时或不确定结果
必须人工复核，禁止自动重试。当前切片不包含 HTTP/OAuth transport、server
安装/更新、MCP resources/prompts 和延迟 Schema 加载。
OS 级进程 sandbox 也保留为单独 hardening 切片。

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
- `ContextManifest` 在不回显私密 segment 正文的情况下暴露上下文来源和预算。
- `ProgramPlan` revision 不可变，run 不能改绑 contract 或 plan。
- retry、alternative-tool 和 approval 恢复会继续所有未完成且可执行的验证 frontier。
- 替代工具只能完成原持久化步骤，不能修改其 ID、依赖或验证要求。
- 可信 runtime acceptance facts 作为 append-only event 持久化，可跨进程重启保留。
- `LongTaskStore` 存储 pending interaction、artifact、side effect，不替代 `TaskStore`。
- `ParkedRunScanner` 只做 parked run 分类，不执行工具、不修改状态。
- `DagExecutionPolicy` 只给出保守调度建议，不执行 DAG step。
- `HarnessRuntime` 只允许一个受限批次并发执行独立、只读且 concurrency-safe 的
  frontier step；其他 frontier 工作保持线性。
- `RunBranchTreeBuilder` 只读投影 fork lineage，并拒绝缺失或循环 parent chain。
- Memory 写入必须来自确定性的显式“记住”语言；模型输出不能静默持久化记忆。
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
- MCP server 只能是 host 配置的本地 stdio 进程，必须具备精确命令批准、精确工具
  白名单、命名环境变量引用、可校验 Schema 和显式生命周期关闭。
- MCP 调用保持 confirm-tier 和 retry-unsafe，只能经过正常 Runtime/outbox 路径，
  并产生 `mcp://server/tool` evidence。

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

核心迁移已经关闭。M20/M23/M24 当前已经提供受限只读并发、保守自动路由、显式
持久记忆、Context Manifest、分支视图、分阶段 Token 预算，以及 seed/stress
路由报告。自动 task 仍由 host 显式开启，而不是默认开启；下一项证据门槛是来自
代表性 host 流量的重复和扩展评估，不再增加新的架构层。

远程 HTTP/OAuth MCP、server 安装/更新、resources/prompts、延迟 Schema 加载、
Skill 下载/更新/删除/执行、cron、OpenGUI、RAG、隐式记忆挖掘和多 Agent 委派
仍明确延后。不得从恢复标签整体搬回任何 deferred 能力。

## 重要边界

仓库根是唯一活跃源码树。旧实现只能通过恢复标签为已批准切片提供行为参考，
`.zlagent/legacy-runtime/` 不得作为源码或规则输入。
