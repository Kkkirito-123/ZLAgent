# re_zlagent 重构路线图

**语言：** [English](./ROADMAP.md) | [简体中文](./ROADMAP.zh-CN.md)

> 权威说明：英文版 `ROADMAP.md` 是 AI 工程执行、阶段状态、进入条件和退出条件的唯一权威来源。本文是供用户阅读的中文翻译，不参与 AI 的规则判断或工程决策。如两者存在差异，以英文版为准。

## 1. 文档目的

这份路线图把 ZLAgent 重构拆成可以验证的工程阶段。它不是功能愿望清单，也不是传统的大型产品 PRD，而是把最小产品定义、迁移历史、执行顺序和验收门槛集中在一个地方，确保旧后端能够被安全替换。

各文档职责保持单一：

- `CLAUDE.md`：AI 工作规则和架构不变量。
- `ROADMAP.md`：阶段顺序、阶段状态和迁移决策。
- `README.md`：面向用户的项目简介和使用方式。
- `CONTRIBUTING.md`：开发流程。
- `*.zh-CN.md`：供用户阅读的同步翻译，不是 AI 权威来源。

不要把路线图全文复制回 `README.md` 或 `CLAUDE.md`，只保留链接。

## 2. 产品定义

### 当前问题

旧 ZLAgent 后端包含有价值的能力，但任务事实、工具执行、故障恢复、产品适配器和可选知识系统之间耦合较重。长任务可能超过聊天上下文寿命，外部动作可能在故障后重复执行，模型自然语言也容易被错误地当成已验证完成。

### 用户和干系人

- 使用 IM-first 个人助理的仓库维护者
- 增加工具、网关、模型和存储适配器的开发者
- 查看、暂停、恢复、取消或 fork 任务的操作人员
- 按仓库规则参与开发的 coding agent

### 目标结果

`re_zlagent` 最终成为可复用 agent harness：能够接收任务、持久化不可变任务契约和计划、执行有边界的工具、跨进程重启恢复、安全等待用户、继续所有未完成工作，并且只依据可信验收证据完成任务。

### 第一版产品 MVP 边界

第一版完整产品流程是：

```text
用户请求
  -> 可信任务契约
  -> 已验证计划
  -> 有边界的工具执行
  -> append-only 事件与 checkpoint
  -> 持久化用户等待或可恢复故障
  -> 重启后继续执行
  -> 可信验收
  -> 结构化结果
```

第一版 MVP 使用单进程 worker、单模型适配器、SQLite 持久化、最小任务提交/状态/控制入口，以及现有文件、URL、消息工具边界。只有本地语义验证稳定后，才进入 PostgreSQL 生产运行。

### 第一版 MVP 明确不做

- 默认 DAG 并发
- 自治多 agent delegation
- 大范围 MCP、cron 或 OpenGUI 迁移
- wiki、Graph-RAG 或 geo 知识系统
- 照搬旧后端目录结构
- 把模型输出、benchmark 分数或健康快照当作完成判定权威

## 3. 状态定义

| 状态 | 含义 |
| --- | --- |
| `LANDED` | 已进入当前分支提交历史，并按原阶段范围完成验证。 |
| `LOCAL` | 已在当前工作树实现并验证，但尚未提交。 |
| `NEXT` | 唯一允许下一步开始的阶段。 |
| `PLANNED` | 已排序的后续工作，进入条件未满足前不能开始。 |
| `DEFERRED` | 明确延后，不在当前关键路径中。 |
| `REMOVED` | 已从当前重构目标中移除，除非新的产品决策重新开启。 |

`LANDED` 不代表实现永远不需要修正。后续审计可以重新打开语义问题，但不能抹掉历史阶段。

## 4. 工程不变量

每个阶段都必须保持以下规则：

1. `HarnessRuntime` 是唯一工具执行生命周期。
2. 每个 run 必须绑定不可变的契约版本和计划版本。
3. 模型可以提出计划和验收要求，但不能声明可信证据、测试通过、时效性或人工批准。
4. 事件和 checkpoint 是 append-only 事实；run 数据行只是当前投影。
5. 阶段完成和任务完成是两个不同判定。
6. 故障恢复必须继续所有必要的未完成步骤，只重试最后一个失败步骤并不足够。
7. 聊天历史永远不是长任务权威状态。
8. 外部副作用必须具备权限、幂等身份、持久化意图和可检查结果状态。
9. 只读模型、进度视图、scanner、eval 和健康监控不能修改执行状态，也不能判定完成。
10. 在 worker 所有权、副作用安全、确定性合并和故障注入测试完成前，不启用并发执行。

## 5. 阶段总览

| 阶段 | 状态 | 目标 |
| --- | --- | --- |
| M1 | `LANDED` | 冻结重构范围和仓库权威。 |
| M2 | `LANDED` | 建立 harness、结构化工具、任务事件、checkpoint 和验收。 |
| M3 | `LANDED` | 建立 app、gateway、operator、facade 和包边界。 |
| M4 | `LANDED` | 增加明确的用户批准和替代工具恢复入口。 |
| M5 | `LANDED` | 增加阶段验证、故障分类、DAG 表达和 eval 基础。 |
| M6 | `LANDED` | 增加长任务计划、投影和重启上下文。 |
| M7 | `LANDED` | 增加长任务 ledger 存储边界。 |
| M8 | `LANDED` | 为 interaction、artifact 和 side effect 增加 SQLite 持久化。 |
| M9 | `LANDED` | 增加只读 parked-run 分类。 |
| M10 | `LANDED` | 增加保守 DAG 执行评估。 |
| M11 | `LANDED` | 把 runtime side-effect 结果记录到 ledger。 |
| M12 | `LANDED` | 增加组合式长任务进度快照。 |
| M13 | `LANDED` | 封闭验收信任和不可变任务契约问题。 |
| M14 | `LANDED` | 持久化计划，并在恢复后继续完整未完成 frontier。 |
| M15 | `LANDED` | 通过 outbox 边界保证外部副作用的崩溃安全。 |
| M16 | `LANDED` | 增加持久 worker 领取、lease、heartbeat 和重试预算。 |
| M17 | `LANDED` | 交付真实任务提交与执行 MVP。 |
| M18 | `LANDED` | 增加 CI、benchmark、故障注入和时延/可靠性门槛。 |
| M19-SKILLS | `LANDED` | 增加受控本地且不覆盖的 Skill 安装。 |
| M19-MCP | `LANDED` | 增加经过批准的本地 stdio MCP 生命周期和动态工具。 |
| M19 | `DEFERRED` | 后续可选能力仍需逐个独立切片迁移。 |
| M20 | `DEFERRED` | 所有前置门槛通过后启用安全 DAG 并发。 |
| M21 | `LANDED` | 把重构提升到根目录并关闭已批准的旧迁移。 |

## 6. 历史阶段

### M1 - 范围和仓库权威

**状态：** `LANDED`

**目标：** 把 `re_zlagent/` 建立为唯一重构工作区，并把旧 backend、旧 tests、OpenGUI 和治理草稿设为参考材料。

**已交付：** 精简 `AGENTS.md`、权威 `CLAUDE.md`、包工作区、独立源码与测试目录，以及禁止盲目复制旧代码的迁移规则。

**证据：** commit `eeeb42f` 和当前架构边界测试。

### M2 - Harness 和任务基础

**状态：** `LANDED`

**目标：** 在产品适配器之前建立确定性的任务契约。

**已交付：** 结构化 `ToolResult`、权限、read-before-write、工作区路径沙箱、任务契约、append-only 事件、checkpoint、恢复策略、`AcceptanceGate`、SQLite TaskStore 和 PostgreSQL TaskStore 语义。

**原退出条件：** 有边界的工具执行和确定性任务验收均有单元测试覆盖。

### M3 - 应用和包边界

**状态：** `LANDED`

**目标：** 防止产品入口形成绕过 harness 的第二条执行路径。

**已交付：** `re_zlagent.*` namespace、app/gateway contract、operator 控制、facade 快照、bootstrap 装配、JSON CLI 控制和本地检查命令。

**证据：** commits `f1f4dc1`、`9acea7d`、`6af3fa5`。

### M4 - 明确恢复入口

**状态：** `LANDED`

**目标：** 让用户批准和替代工具恢复可以检查，并统一经过正常 runtime 和 acceptance 路径。

**已交付：** approval service、用户批准恢复、明确替代工具恢复、失败 checkpoint 和 append-only 恢复事件。

**证据：** commits `df828c4`、`28e6055`。

### M5 - 验证、DAG 与评估基础

**状态：** `LANDED`

**目标：** 分离阶段完成与任务完成，并在不启用不安全并发的情况下表达依赖。

**已交付：** `PlanStep`、`StepVerifier`、故障扰动分类、`PlanDAG`、进度视图、eval scenario、健康快照、trace、doctor 和 support bundle。

**已知修正项：** 后续审计发现可信验收事实和完整剩余计划恢复尚未真正封闭，由 M13 和 M14 负责修正。

### M6 - 长任务计划与上下文模型

**状态：** `LANDED`

**目标：** 不依赖保留聊天轮次来表示长任务。

**本地已交付：** `ProgramPlan`、`ProgramPhase`、`LongTaskProjector`、`LongTaskProjection`、`PendingInteraction`、`ArtifactRecord`、`SideEffectRecord` 和 `ContextPackBuilder`。

**证据：** 定向测试以及完整 240 项本地检查。

### M7 - 长任务 ledger 边界

**状态：** `LANDED`

**目标：** 把长任务 interaction、artifact 和 side effect 与核心 contract、run、event、checkpoint 分离。

**本地已交付：** `LongTaskStore` 和 `InMemoryLongTaskStore`。

### M8 - SQLite 长任务持久化

**状态：** `LANDED`

**目标：** 本地进程重启后仍保留长任务 ledger 记录。

**本地已交付：** `SqliteLongTaskStore`，包含 pending interaction、task artifact 和 side-effect 表。

### M9 - Parked-run 分类

**状态：** `LANDED`

**目标：** 在不修改状态的情况下，识别暂停、等待用户、可重试、终态和需要操作员处理的 run。

**本地已交付：** `ParkedRunScanner` 和面向 scheduler 的只读模型。

### M10 - DAG 执行评估

**状态：** `LANDED`

**目标：** 识别保守可执行 frontier，但不进行调度。

**本地已交付：** `DagExecutionPolicy` 以及冲突和只读安全原因。

### M11 - Side-effect 结果账本

**状态：** `LANDED`

**目标：** 使用 run 范围幂等键记录对外可见结果。

**本地已交付：** normal、retry、alternative-tool 和 approval 路径都会写 applied side-effect 记录。

**已知限制：** 当前记录发生在外部动作之后，它只是审计账本，还不是崩溃安全 outbox。M15 负责修正。

### M12 - 长任务进度视图

**状态：** `LANDED`

**目标：** 为 app/operator 提供统一的只读长任务快照。

**本地已交付：** `LongTaskProgressReader`，组合 task progress、DAG projection、parked state 和 execution assessment。

## 7. 后续执行阶段

### M13 - 验收信任与不可变任务事实

**状态：** `LANDED`

**目标：** 防止模型输出或后续契约覆盖错误完成任务，或重新解释历史 run。

**MVP 范围：**

- 分离 planner 提出的验收要求与 runtime 产生的可信事实
- 拒绝模型提供 evidence、passed tests、freshness 和 human approval
- 至少要求一个 required acceptance criterion
- 保存后的任务契约必须不可变或显式版本化
- 每个 run 绑定创建时使用的准确契约版本
- 在 memory、SQLite、PostgreSQL adapter 中保持相同行为
- 为每条已发现的错误完成路径增加回归测试

**不做：** worker 调度、完整计划继续执行、API、可选能力迁移和并发。

**进入条件：** M1-M12 行为保持绿色，当前 M6-M12 本地改动必须完整保留。

**退出条件：**

- 零步骤模型计划不能自行声明测试或批准后完成
- 全部 optional 的契约必须被拒绝
- 同一 contract identity 下保存不同内容必须被拒绝或生成不同 revision
- 历史 run 永远解析到原始 contract revision
- memory、SQLite、PostgreSQL adapter contract test 全部通过
- 完整仓库检查通过

**本地证据：** planner 中的 acceptance 字段已被拒绝；`AgentPlan` 不再包含 runtime facts；contract 必须至少有一个 required criterion；三种存储都执行不可变 contract identity；244 项测试和完整本地检查通过。

### M14 - 持久化计划与完整恢复

**状态：** `LANDED`

**目标：** 恢复完整未完成任务，而不是只恢复最后一个失败工具。

**MVP 范围：**

- 持久化不可变 `AgentPlan`/`ProgramPlan` revision
- run 和 checkpoint 绑定 plan revision 与当前 frontier
- 从 event、checkpoint 和持久化计划重建剩余工作
- retry、alternative-tool 和 approval 统一回到一个 continuation 路径
- 由 runtime/app service 创建和解决持久 pending interaction
- 把 `ContextPack` 注入 continuation planning

**不做：** 后台 worker 和并行 DAG 执行。

**退出条件：**

- 三步任务第二步失败时，恢复必须按要求执行第二步和第三步
- 已完成步骤不能重复，除非 policy 明确允许重复
- SQLite 重启后无需聊天历史即可重建同一 frontier
- 所有 open interaction 都绑定 checkpoint 和 resume token
- 任何恢复路径都不能绕过 step verification 和 final acceptance

**本地证据：** memory、SQLite、PostgreSQL adapter 均可保存不可变计划；run
的 contract/plan 绑定不可修改；retry、alternative-tool、approval 共用同一条
步骤验证路径；`ContextPack` 驱动剩余 frontier；可信 acceptance facts 通过
append-only event 跨重启保留；持久 interaction 可在批准时解决；三步续跑、
恶意替代和 SQLite 重启测试均通过；258 项测试和完整本地检查通过。

### M15 - 崩溃安全 Side-effect Outbox

**状态：** `LANDED`

**目标：** 防止崩溃和重试造成重复或不可见外部动作。

**MVP 范围：** 动作前持久化意图、向 adapter 传递稳定幂等键、outbox 状态转换、对账，以及明确的 uncertain/manual-review 状态。

**退出条件：** 在 dispatch 前、dispatch 后和结果提交前注入崩溃，都不能静默重复已确认动作；恢复可以区分 planned、applied、confirmed、failed、reverted 和 uncertain。

**本地证据：** side-effect 工具在 dispatch 前声明确定性 intent；逻辑幂等键在
retry 间保持稳定；message adapter 接收该键；文件写入通过内容哈希对账；memory
和 SQLite store 使用 compare-and-set 执行状态转换；applied result snapshot 可在
重启后重放；非幂等 dispatch 中断进入 `uncertain`，必须基于证据人工对账；故障
注入覆盖 intent 已持久化、dispatch 已返回和 result commit 前三个边界；270 项
测试和完整本地检查通过。

### M16 - 持久 Worker 与 Run 所有权

**状态：** `LANDED`

**目标：** 使用明确所有权和有限恢复能力在后台执行长任务。

**MVP 范围：** list/claim API、lease、heartbeat、lease expiry、retry budget、backoff、dead-letter/manual-review、协作式 pause/cancel，以及 PostgreSQL long-task ledger adapter。

**不做：** 多节点并行 DAG 执行。

**退出条件：** 同一 run 只有一个 live worker；过期 lease 可被接管；重启不丢进度；重试达到预算后停止；waiting-user run 不占用 worker lease。

**本地证据：** memory、SQLite、PostgreSQL TaskStore adapter 均实现 exclusive
claim、heartbeat、过期接管、status compare-and-set、backoff 和 retry budget；
PostgreSQL 已实现 long-task/outbox ledger；worker 把 created、running、recovering
run 统一交给 `HarnessRuntime`；SQLite 重启保留 plan、progress 和 lease state；
waiting-user、paused、cancelled run 会释放所有权；协作取消会在下一 step 前停止；
parked-run scanner 能看到 dead-letter；290 项测试和完整本地检查通过。

### M17 - 真实产品执行 MVP

**状态：** `LANDED`

**目标：** 让 harness 可以从真实本地产品入口使用。

**MVP 范围：** 任务提交 CLI/API、显式模型和密钥配置、感知工具 schema 的 planner、request context、结构化状态/结果，以及一个具体 gateway 或本地 adapter。

**退出条件：** 全新 checkout 可以配置模型、提交任务、查看进度、回答批准请求、跨重启继续，并通过文档命令获得已验证结果。

**本地证据：** `HarnessRuntime.submit` 和 `AgentOrchestrator.submit` 在不执行工具的
情况下持久化不可变 contract、plan、request context 和 created run；本地 JSON
adapter 提供 submit、status、work、approval 和 result；OpenAI-compatible 配置只从
明确环境变量解析密钥；planner prompt 注入 host tool schema，并拒绝不可用工具或
模型自行授予确认权限；模型 contract identity 会重新绑定到 host request；SQLite
端到端测试在提交、worker 停驻、resume-token 批准和已验证文件结果之间反复关闭并
重开应用；299 项测试和完整本地检查通过。

### M18 - 可靠性、评估与 CI 门槛

**状态：** `LANDED`

**目标：** 把正确性、恢复和时延要求变成可重复的发布证据。

**MVP 范围：** 持久 benchmark corpus、短计划和长任务 suite、故障扰动测试、崩溃/重启测试、时延预算、类型/lint 检查和 CI。

**退出条件：** CI 能阻止语义回归；benchmark 结果具有版本；错误完成、重复副作用、遗弃 run 和时延回归都有明确阈值。Benchmark 始终只是观察者，不能成为完成判定权威。

**本地证据：** 版本化 corpus `2026.07.1` 通过 6 个真实生命周期场景覆盖短计划、
长任务 retry continuation、outbox 崩溃重放、SQLite 批准重启和过期 lease 接管；
发布阈值要求通过率 100%、错误完成 0、重复副作用 0、遗弃 run 0，并设置每个 case
和 15 秒 suite 时延预算；本机 6/6 benchmark 在 0.1 秒内完成；统一 check 运行
308 项测试、benchmark、compileall、Ruff、对 82 个源码文件执行 mypy、CLI smoke
和 package dry-run；根目录 `.github/workflows/quality.yml` 在 Python 3.11 与
3.13 运行同一命令。

**当前加固证据：** host 已在权限判断和副作用规划前强制校验工具输入 schema；
运行时不支持的 schema 关键字会在注册阶段失败；参数不合法的模型计划默认最多进行
一次受控修复；超长计划和完全相同的重复动作会在持久化前失败。聚焦 planner/tool
测试、316 项完整测试、对 83 个源码文件执行的 mypy，以及 6/6 release benchmark
均在本地通过，统一质量命令也已通过；本次修正在当前分支中为 `LANDED`。

### M19 - 受控可选能力迁移

**状态：** `DEFERRED`

**目标：** 只在核心 MVP 可靠后迁移产品真正需要的能力。

每项能力都必须是单独批准的切片，并拥有自己的边界测试。候选顺序为 durable memory、skill lifecycle、MCP、cron/scheduled jobs 和 OpenGUI。禁止在一次重构中全部引入。

**已批准切片 M19-SKILLS（`LANDED`）：** 当 host 配置彼此独立的导入目录和托管
目录后，可以受控安装本地 Skill。confirm-tier 工具会在原子且不覆盖的安装前校验
根 manifest、包限制、符号链接、路径边界、manifest id 和静态安全扫描；它声明
确定性的文件系统 intent，通过 durable outbox 执行，返回结构化证据，刷新只读
inventory，并把字节完全相同的内容当作崩溃恢复时的幂等重放。

**M19-SKILLS 不做：** 网络或仓库下载、Skill 执行、curation、更新、删除、依赖安装、
MCP 和远程 registry。MCP 不包含在 Skill 改动中，必须通过下面单独批准的切片交付。

**M19-SKILLS 证据：** 聚焦测试覆盖 Hermes 和 legacy 包、显式批准、结构化证据、
路径/符号链接拒绝、manifest/来源稳定性、危险文本、包限制、冲突保留、相同内容重放、
inventory 刷新和 dispatch 崩溃恢复。本地统一质量门槛通过 328 项测试、6/6 release
benchmark、compileall、Ruff、对 85 个源码文件执行的 mypy、CLI smoke 和包 dry-run。

**已批准切片 M19-MCP（`LANDED`）：** host 可以加载 fail-closed JSON 配置，其中只
允许明确批准的本地 stdio 命令、每个 server 的精确工具白名单、命名环境变量引用和
受限 timeout。官方稳定 Python SDK（`mcp>=1.28,<2`）在独立 event-loop thread
中负责 initialize、能力协商、工具发现/调用和 subprocess 正常关闭。白名单远端工具
会转换为 confirm-tier、retry-unsafe、outbox-required 的 Harness 工具，只能经过
唯一 Runtime 生命周期执行，并产生 `mcp://server/tool` evidence。

**M19-MCP 不做：** server 安装/更新、远程 HTTP/OAuth transport、MCP
resources/prompts、信任远端 read-only 或 idempotency annotation、不确定结果后的
自动重试，以及延迟 Schema 加载/tool search。Token-aware Schema 发现和指标保留为
后续独立切片。本地 subprocess 不是 OS sandbox，仍以调用者权限运行；进程 sandbox
也必须作为后续独立 hardening 切片。

**M19-MCP 证据：** 聚焦测试覆盖严格配置、精确命令批准、只按 credential 名称解析且
不持久化值、工具白名单、不支持 Schema 拒绝、provider-safe alias、真实 stdio
initialize/list/call/close、Runtime 批准继续执行、durable outbox 确认、`mcp://`
来源，以及把真实 request timeout 映射为人工复核。本地统一质量门槛通过 338 项测试、
6/6 release benchmark、compileall、Ruff、对 89 个源码文件执行的 mypy、CLI smoke
和包 dry-run。

M19 阶段仍为 `DEFERRED`：任一受限切片落地都不会自动激活其余可选能力。

Wiki、Graph-RAG 和 geo 当前状态为 `REMOVED`。重新引入必须先形成新的产品决策并修改路线图。

### M20 - 安全 DAG 并发

**状态：** `DEFERRED`

**目标：** 在不削弱任务事实和副作用安全的前提下降低延迟。

**进入条件：** M15、M16、M18 已完成，顺序执行的所有权和故障测试已经证明。

**初始范围：** 仅允许无依赖、只读、无副作用步骤并发，并具备确定性结果合并和顺序 fallback。

**退出条件：** 冲突写操作不能并发；取消和失败确定性传播；同一确定性场景下并行和顺序执行产生等价的 accepted outcome。

### M21 - 迁移关闭与旧源码删除

**状态：** `LANDED`

**目标：** 让 `re_zlagent` 成为唯一维护的 ZLAgent 实现。

**进入条件：**

- M13-M18 已完成
- 每个旧能力都已标记为 replaced、明确 removed，或带负责人和原因的 deferred
- 产品需要的 M19 切片已完成；已批准的本地核心关闭不要求任何 M19 切片
- 源码和测试不再 import 旧 backend
- 端到端和 benchmark 门槛通过
- 已创建仓库备份/tag 和回滚说明
- 所有旧目录脏工作区路径都已审查，并被保存到经批准的源码快照，或明确保留在删除范围之外
- credential 和 runtime data 不得进入任何源码快照
- 用户查看最终 ledger 后明确批准删除

**退出条件：** 旧源码在一次聚焦改动中删除；全新 checkout 验证通过；回滚证据保留在被删除目录之外。

**已执行仓库根关闭步骤：**

1. 38 项旧目录脏改动已审查并保存到 commit `54e47b2`；快照不包含 `.env`、
   `workspace/` 或重构源码。
2. 完整且已验证的重构保存到 commit `510436a`。Annotated tag
   `pre-rebuild-root-promotion` 指向该 commit，因此同时保留已审查旧状态和重构 tree。
3. 重构 package、tests、规则、README、license、ignore policy 和 workflow 已从
   `re_zlagent/` 提升到仓库根。
4. 已删除 tracked 旧产品材料：`backend/`、`OpenGUI-main/`、旧 tests/config/scripts、
   Docker/启停脚本、旧运行文档、旧 requirements、旧 notices 和旧媒体文件。
5. `.env`、`workspace/`、数据库、日志、credential 和其他 runtime data 未被删除或
   提交。约 1.9 GB 被旧规则忽略的构建/运行产物原样移动到
   `.zlagent/legacy-runtime/`。
6. Commit `902ac53` 记录根目录提升和 tracked 旧源码删除。正式根目录与 detached
   clean checkout 都通过 release gate：308 项测试、6/6 benchmark、compileall、
   Ruff、对 82 个源码文件执行 mypy、CLI smoke 和 package dry-run；安装后的 console
   entry point 也通过检查。
7. 代码通过 annotated promotion 前 tag 回滚；runtime data 必须使用独立备份恢复。

**关闭前仓库审计结果（2026-07-11）：** Git 跟踪 2,338 个文件，其中
`OpenGUI-main/` 1,954 个、`backend/` 229 个、`re_zlagent/` 107 个、
`workspace/` 23 个，另有 25 个根级旧产品文件。候选旧产品改动影响 2,208 个
tracked 文件；`re_zlagent/` 外存在 31 个修改文件、3 个 tracked 删除和 4 个
untracked 路径。已批准关闭把这些改动保存到 Git，并把活跃 index 缩减为 171 个
tracked 文件，其中包含明确保留的 23 个 `workspace/` 文件。

M21 因此满足退出门槛。当前没有自动开始的下一阶段；M19 或 M20 只能在新的产品决策
和批准后开始。

## 8. 能力决策

这是 M21 删除 ledger。`DEFERRED` 表示删除时会有意移除旧实现，未来重新引入时
必须遵守当前 harness 边界；它不表示该能力已经迁移完成。

| 旧能力区域 | 决策 | 当前替代或原因 | 负责人/门槛 |
| --- | --- | --- | --- |
| task contract、event、checkpoint、acceptance | `REPLACED` | 不可变 plan、append-only truth、可信验收 | M13-M14 |
| 长任务 continuation 和 worker ownership | `REPLACED` | context pack、lease、retry budget、dead-letter | M14-M16 |
| side-effect 安全 | `REPLACED` | 确定性 intent 和 crash-safe outbox | M15 |
| 本地模型规划和 operator 流程 | `REPLACED` | schema-bounded JSON planner 和本地产品 CLI | M17 |
| SQLite/PostgreSQL 任务语义 | `REPLACED` | TaskStore/LongTaskStore；真实 PostgreSQL DSN 仍需环境验证 | M13-M16 |
| 文件读写、URL 读取、消息发送、工具发现 | `REPLACED` | bounded tool、permission、evidence、read-before-write | M2-M4 |
| confirmation 和 run control | `REPLACED` | pending interaction、resume token、pause/resume/cancel/fork | M3-M5、M14 |
| observability、doctor、support bundle、progress | `REPLACED` | 只读 facade、trace、health 和 progress snapshot | M6-M12 |
| 发布评估 | `REPLACED` | 版本化语义/恢复/时延 corpus 和质量门槛 | M18 |
| 微信、企业微信、Webhook 具体 gateway | `DEFERRED` | 当前只有标准 gateway contract 和本地 CLI；删除会移除在线 IM 入口 | 删除前产品决策 |
| FastAPI route 和部署脚本 | `DEFERRED` | 当前 MVP 没有 HTTP 产品服务 | 删除前产品决策 |
| durable memory 和 retrieval | `DEFERRED` | 已有 versioned in-memory 边界；durable provider/retrieval 未迁移 | M19，用户/产品负责人 |
| skill curator、consolidation、review、usage 生命周期 | `DEFERRED` | 受控本地且不覆盖的安装已落地；curation、更新、删除、执行和 usage 生命周期仍延后 | M19，用户/产品负责人 |
| 本地 stdio MCP transport 和动态工具生命周期 | `REPLACED` | 精确命令批准、工具白名单、命名 credential、confirm/outbox/evidence 边界 | M19-MCP |
| MCP 安装/更新、远程 HTTP/OAuth、resources/prompts、延迟 Schema 加载 | `DEFERRED` | 需要独立 supply-chain、auth、discovery 和 token-eval 切片 | M19，用户/产品负责人 |
| cron 和定时发送 | `DEFERRED` | 需要 durable scheduler ownership 和 delivery 语义 | M19，用户/产品负责人 |
| OpenGUI 和 Android 执行 | `DEFERRED` | 已明确排除在核心重构之外 | M19，用户/产品负责人 |
| 代码执行、Web 搜索、delegation/subagent | `DEFERRED` | 高风险或产品特定工具需要独立 sandbox/acceptance 切片 | 用户/产品负责人 |
| plugin、rich rendering、Redis cache、prompt cache | `DEFERRED` | 本地产品 MVP 不需要 | 用户/产品负责人 |
| wiki、Graph-RAG、geo、travel visited-map | `REMOVED` | 已明确从当前目标移除 | 恢复时重开路线图 |
| 默认 DAG 并发 | `DEFERRED` | 顺序语义已证明，并发必须单独评审 | M20 |

**删除决策已解决：** 2026-07-11 用户批准本地核心关闭、本地 snapshot commit 和
tag、根目录提升以及不推送的 tracked 旧源码删除。Deferred 能力会继续从活跃产品
中缺席，直到单独批准的 M19/M20 工作通过当前边界重新实现。

## 9. 完成定义

一个阶段只有在以下条件全部成立时才算完成：

1. 范围和非范围仍然准确。
2. 必需代码和迁移都通过所属边界实现。
3. 定向测试覆盖成功、拒绝、失败和恢复路径。
4. 完整 `re_zlagent.check` 通过。
5. 没有覆盖或回滚无关用户改动。
6. 英文权威文档已更新，中文用户文档已同步。
7. 风险和明确延后的工作可见。
8. 状态必须有证据：未提交工作保持 `LOCAL`，只有已提交且验证通过的工作才能变为 `LANDED`。

一个任务 run 只有在以下条件全部成立时才算完成：

- 每个 required step 已通过，或按照契约支持的 policy 明确跳过
- 每个 required acceptance criterion 都有可信证据
- 必需 freshness window 有效
- 必需 human approval 来自已认证 interaction 路径
- 没有未解决的 required interaction
- side effect 处于可检查终态
- `AcceptanceGate` 根据这些事实返回 accepted

## 10. 执行纪律

- 严格按阶段顺序工作，不能把后续能力提前塞进当前阶段。
- 每次改动只服务一个工程目标，每个模块只有一个明确 owner boundary。
- 退出条件失败就重新打开当前阶段，不能用说明文字绕过。
- 通过新事件、新 checkpoint 或显式 revision 修正状态，不能删除历史。
- 先更新英文权威文件，并在同一次文档改动中同步中文翻译；AI 不能使用中文翻译驱动工程决策。
- M21 后不得整体恢复已删除旧源码。只能为单独批准的能力切片检查恢复标签。
