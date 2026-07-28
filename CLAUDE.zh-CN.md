# ZLAgent 项目规则中文版本

这是 `CLAUDE.md` 的中文配套版本，仅用于用户阅读和协作理解。AI 工程执行必须使用英文 `CLAUDE.md`；本文件不参与规则判断，发生差异时以英文版为准。

## 1. 工作区定位

当前仓库根目录是正式的 ZLAgent 重构项目。它不是旧 `backend/`，也不是
`OpenGUI-main/` 的 fork。活跃 Git tree 只保留重构项目和明确保留的 `workspace/` 内容。

提升前的旧实现可通过 annotated tag `pre-rebuild-root-promotion` 恢复。本地生成物可能
保存在 `.zlagent/legacy-runtime/`，但它们属于运行数据，不是源码或规则事实源。

当前工作区采用 OpenGUI 风格的规则结构：

```text
AGENTS.md       agent 入口规则
CLAUDE.md       项目事实源
ROADMAP.md      阶段、状态和迁移门槛
README.md       人类可读项目状态
CONTRIBUTING.md 开发流程
src/            实现
tests/          可执行边界检查
```

不要新增长期存在的 `docs/`、`plans/`、`reports/` 目录，除非用户明确要求。

## 2. 默认工作规则

- 默认使用中文回复、计划、开发说明和复盘。
- 在本仓库工作前，先读 `AGENTS.md` 和 `CLAUDE.md`。
- 涉及阶段开发、迁移或能力选择时，再阅读英文 `ROADMAP.md`。
- AI 只使用英文权威文件。所有 `*.zh-CN.md` 都是用户阅读翻译，不作为 AI 工作上下文或事实源。
- 英文权威文档更新时，应在同一次文档改动中同步中文用户版本。
- `.env`、`.zlagent/`、`workspace/logs/` 和 `workspace/runtime/` 属于本地数据；
  未经明确批准不得读取、删除、移动、stage 或提交。
- 每次只服务一个明确目标。
- 新功能、重构、删除、依赖变更、数据库变更、批量修改前，必须先写中文计划并等待确认。
- 简单查询、读文件、运行测试、解释代码、修明显小 bug 可以直接执行。
- 不覆盖、回滚或删除用户已有改动，除非用户明确确认风险。
- 每次代码改动后必须运行合适验证；不能验证时要明确说明。

计划必须包含：

```text
本次目标
用户 / 干系人
MVP 范围
不做什么
预计文件结构或改动范围
验收标准
风险与取舍
```

复盘必须包含：

```text
改了什么
改了哪些文件
如何验证
发现的问题
剩余风险
可沉淀的新规则
```

## 3. 架构方向

目标是构建 DeerFlow 风格的 agent harness，并适配 ZLAgent 的 IM-first 个人助理场景。

目标层次：

```text
gateway          输入/输出渠道
app              API、IM adapter、operator surface
harness          可复用 agent runtime、policy、tool、sandbox、tasking
facade           给 app/gateway 的只读 inventory 和 runtime snapshot
agent            lead-agent 编排和可选 delegation
tasking          contract、event、checkpoint、acceptance gate
tools            带权限、证据、错误边界的工具能力
skills           可复用工作流
sandbox          文件系统/进程边界
memory           持久事实和笔记
observability    trace、eval、doctor、support bundle
storage          持久化 adapter 和 schema 边界
progress         从存储状态读取的只读进度快照
```

依赖方向：

```text
app -> harness
gateway -> app -> harness
harness -> tools / sandbox / tasking / memory / storage / observability
app/gateway -> HarnessFacade
```

禁止的捷径：

- tool 直接调用 agent loop
- storage 依赖 agent 逻辑
- memory 直接发送 gateway message
- harness import app code
- 绕过正常 run lifecycle 的执行路径
- 由模型文本声明任务完成，而不是由 acceptance evidence 判定

## 4. 当前核心实现

### `gateway`

- `DeliveryTarget`
- `IncomingMessage`
- `OutgoingMessage`
- `GatewayAdapter`

### `app`

- `AgentApplication`
- `ApplicationDispatcher`
- `ApplicationBootstrapConfig`
- `ApplicationContainer`
- `ApplicationRuntimeContainer`
- `LocalTaskAdapter`
- `OperatorService`
- `ApprovalService`
- `build_application_container`
- `build_application_runtime`
- `run_cli`

### `harness/tasking`

- `TaskContract`
- `TaskRun`
- `TaskEventLog`
- `Checkpoint`
- `AcceptanceGate`
- `RecoveryPolicy`
- `ResumePolicy`
- `PlanDAG`
- `StepVerifier`
- `DagExecutionPolicy`
- `ProgramPlan`
- `LongTaskProjector`
- `PendingInteraction`
- `ArtifactRecord`
- `SideEffectRecord`

### `harness/storage`

- `TaskStore`
- `InMemoryTaskStore`
- `SqliteTaskStore`
- `PostgresTaskStore`
- `PostgresLongTaskStore`
- `LongTaskStore`
- `InMemoryLongTaskStore`
- `SqliteLongTaskStore`

### `harness/runtime`

- `HarnessRuntime`
- `HarnessRuntime.submit`
- `RuntimeSubmission`
- `RunControlService`
- `ContextPackBuilder`
- `ParkedRunScanner`
- `DurableWorker`
- `resume_from_checkpoint`
- `resume_with_alternative_tool`
- `resume_with_user_approval`

### 其他模块

- `agent/`：planner 和 orchestrator
- `model/`：模型 provider adapter
- `memory/`：持久 memory 和 prompt context
- `skills/`：skill loader 和 safety guard
- `observability/`：trace、doctor、support bundle
- `evals/`：benchmark runner 和 health monitor
- `evals/` 同时包含版本化 release corpus、语义/恢复/时延门槛
- `progress/`：任务进度和长任务进度快照
- `tools/`：工具协议、权限、内置文件/消息/URL 工具
- `tools/schema_validation.py`：运行时工具参数校验和受支持 schema 边界

## 5. 关键语义

- `ToolResult` 必须包含 `status`、`error_type`、`recoverable_by_model`、`recommended_next_action`、`source`、`evidence`、`side_effects`。
- 工具 schema 不只是 prompt 提示。`ToolRegistry` 必须在注册时拒绝运行时不支持的
  schema 关键字，并在权限判断和副作用规划前执行不带类型强转的参数校验。
- `TaskEventLog` 必须 append-only，并支持 idempotency key。
- `Checkpoint` 存储可恢复状态和 failure envelope。
- `ResumePolicy` 只允许明确可重试的 checkpoint 自动恢复。
- `PlanStep` 和 `StepVerifier` 只判断阶段完成，不判断任务完成。
- `PlanDAG` 只验证依赖和线性顺序，不调度并发执行。
- `ProgramPlan` 只组织长任务阶段，不执行工具。
- `ProgramPlan` revision 必须在执行前持久化，并按 id 保持不可变。
- run 的 contract 和 plan 绑定不可变；存储 adapter 必须拒绝跨 contract plan 和重新绑定。
- `PendingInteraction` 是用户/操作员等待点，必须绑定 checkpoint 和 resume token。
- `ContextPackBuilder` 从 `TaskStore`、artifact 和 pending interaction 重建上下文。
- retry、alternative-tool 和 approval 必须回到同一 continuation 路径，由持久化 `ContextPack` 驱动并执行全部剩余可验证 frontier step。
- 替代工具必须使用原持久化 step identity，不能弱化依赖或验证要求。
- `LongTaskStore` 存储 pending interaction、artifact、side effect，不替代 `TaskStore`。
- side-effect 工具必须在 dispatch 前声明确定性 intent、设置 `outbox_required`，并通过 `SideEffectOutbox` 执行。
- 逻辑 side-effect key 绑定 run id、不可变 plan revision 和原 step id；retry/recovery 必须向下游复用同一 key。
- `MessageSender` adapter 必须接收并按稳定 idempotency key 去重。
- outbox 使用 compare-and-set 在 `planned`、`dispatching`、`applied`、`confirmed`、`failed`、`reverted`、`uncertain` 间转换。
- tool result event 持久化后，`applied` 才能变为 `confirmed`。
- `uncertain` 必须显式对账；确认成功时必须提供可重放的可信 `ToolResult`。
- 未声明 side effect 必须隔离为 `uncertain`，不能通过验收。
- `ParkedRunScanner` 只分类 parked run，不执行工具、不改状态。
- `RunLease` 管理 worker identity、token、heartbeat、expiry、attempt count、retry budget、backoff 和 dead-letter；这些字段不进入 `TaskRun.status`。
- durable adapter 中的 TaskStore claim、heartbeat、release 和 run-status transition 必须使用原子 compare-and-set。
- `DurableWorker` 只能调度和 heartbeat；created/running recovery 与 checkpoint retry 必须经过 `HarnessRuntime`。
- `HarnessRuntime.submit` 只持久化不可变 contract、plan、request context 和 created run，不能执行工具。
- active lease 阻止其他 worker；过期 lease 可接管；waiting-user、paused、cancelled、terminal run 不可领取。
- retry budget 耗尽后进入 durable dead-letter/manual-review，禁止无限重试。
- `DagExecutionPolicy` 只给保守调度建议，不执行 DAG step。
- `LongTaskProgressReader` 是只读进度快照。
- `HarnessRuntime` 是唯一工具执行骨架。
- `AcceptanceGate` 是任务完成的唯一判定入口。
- `AgentPlan` 只包含验收要求和 runtime step，不能包含可信验收 facts。
- 模型规划只能选择 host 提供 schema 的工具；不可用工具和模型授予的 confirm 权限必须拒绝。
- `JsonPlanPlanner` 默认最多进行一次受控修复调用；修复后的完整计划必须重新校验，
  并在持久化前限制计划步数和完全相同的重复工具动作。
- 模型提出的 contract identity 和 goal 在持久化前必须重新绑定到可信 host request。
- `OpenAICompatibleModelConfig` 只保存非密钥设置，并从明确命名的环境变量解析 API key。
- `RuntimeAcceptanceFacts` 只能由受信 host/runtime 或 verifier 边界构造，planner 和模型输出不能提供。
- 可信 runtime acceptance facts 必须保存为 append-only event，避免重启改变验收事实。
- 每个任务契约必须至少包含一个 required acceptance criterion。
- `TaskStore.save_contract` 按 contract id 不可变：完全相同内容可以幂等重放，不同内容必须使用新 id。

## 6. 当前存储基线

推荐长期拆分：

```text
task_contracts       稳定目标和验收契约
task_events          append-only 生命周期和工具/eval 证据
task_checkpoints     可恢复快照
task_runs            当前状态投影
pending_interactions 用户/操作员等待点和 resume token
task_artifacts       artifact refs 和 evidence lineage
side_effect_ledger   对外副作用幂等账本
```

当前已实现：

```text
SqliteTaskStore
SqliteLongTaskStore
PostgresTaskStore
PostgresLongTaskStore
```

统一质量命令为 `python -m re_zlagent.check`。它必须运行单测、release benchmark、
compile、Ruff、mypy、CLI smoke 和 package validation。Benchmark 只能观察 runtime
事实，不能替代 `AcceptanceGate`；错误完成、重复逻辑副作用、遗弃 run 或时延超预算
都必须让发布门槛失败。

## 7. 路线图权威

阶段顺序、当前状态、能力决策、进入条件、退出条件和最终旧源码删除门槛，统一由英文 `ROADMAP.md` 管理。本文不重复维护这些内容。

仍由工程规则约束的迁移原则：

- 不得把恢复标签中的旧 `backend/` 当作文件复制目标。只能为已批准能力切片检查旧行为，
  再决定保留、替换、延期或移除。
- 严格按照路线图顺序工作，每次批准的改动只服务一个工程目标。
- 阶段状态只能依据实现和验证证据推进，模型文本、状态总结和开发意图都不是证据。
- 后续可以修正历史阶段，但不能删除其历史。
- wiki、Graph-RAG 和 geo 已从当前目标中移除，除非明确重新开启路线图。
- 重新引入任何已删除旧能力，都必须先形成新的路线图决策并补当前边界测试。

## 8. 敏感区域

这些目录属于高风险区域：

- `src/re_zlagent/gateway/`
- `src/re_zlagent/app/`
- `src/re_zlagent/app/bootstrap.py`
- `src/re_zlagent/app/operator.py`
- `src/re_zlagent/harness/tools/`
- `src/re_zlagent/harness/tasking/`
- `src/re_zlagent/harness/storage/`
- `src/re_zlagent/harness/runtime/`
- `src/re_zlagent/harness/agent/`
- `src/re_zlagent/harness/model/`
- `src/re_zlagent/harness/memory/`
- `src/re_zlagent/harness/skills/`
- `src/re_zlagent/harness/observability/`
- `src/re_zlagent/harness/evals/`
- `src/re_zlagent/harness/progress/`
- `src/re_zlagent/harness/facade.py`

跨层协议变更必须补两侧测试。

## 9. 验证命令

默认验证：

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=src python -m re_zlagent.check
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

## 10. 旧能力恢复规则

M21 关闭后不得整体恢复旧源码。未来只能为单独批准的 M19/M20 切片检查恢复标签，
并通过当前架构边界重新实现和验收。
