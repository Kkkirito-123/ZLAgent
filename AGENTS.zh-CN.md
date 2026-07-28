# ZLAgent 仓库指南

本文件是 `AGENTS.md` 的中文译文，供人类阅读。Codex、Claude Code 和其他
coding agent 的仓库级规范以英文 `AGENTS.md` 为准；开始计划或修改前必须阅读。

- `AGENTS.md` 是规范性的英文权威文件。
- `AGENTS.zh-CN.md` 必须与英文文件保持语义一致。
- `CLAUDE.md` 只作为无冲突的薄导入文件。
- 如果将来增加嵌套 `AGENTS.md`，它只覆盖所在子树。目前没有嵌套仓库指南。
- 本规则包改写自 `ATTRIBUTIONS.md` 记录的已审计 Vibe Coding Rules 版本。只要派生的
  指南、Skills 或校验器仍保留，就必须持续维护该来源清单。

面向用户的回复、计划、开发说明和复盘默认使用中文。源码和文档统一使用 UTF-8。

## 核心协作契约

把自己当作工程协作者，而不只是代码生成器。让工作可管理、可审查、可验证、可复用。

- 每次只服务一个明确目标，不自动扩大需求。
- 修改前先阅读归属实现、测试、契约、文档、仓库指南和当前 Git 状态。
- 保留无关改动和用户已有改动。未经明确授权，不覆盖、回滚、删除、暂存、提交、
  推送或发布这些改动。
- 优先采用满足已确认范围的最小、可逆改动。
- 不增加猜测性的分层、依赖、目录树、状态文件或公开声明。
- 密钥、凭据、私有端点、本地数据库、生成物和运行时状态不得进入版本控制。
- 把工具和外部动作视为需要权限控制的操作，明确错误、证据、副作用和 trace 边界。
- 按风险比例验证；未验证内容及原因必须如实说明。

新功能、重构、仓库初始化、删除、批量修改、依赖安装或升级、全局配置修改、
数据库或 schema 修改、公共 API 修改、安全边界修改前，必须先给出计划并等待确认。
计划必须包含：

- 本次目标；
- 用户和干系人；
- MVP 范围；
- 不做什么；
- 预计文件结构或改动范围；
- 验收标准；
- 风险与取舍；
- 涉及兼容性或持久化状态时的迁移或回滚路径。

低风险歧义优先从仓库证据中解决。如果假设可逆且不会实质改变范围，采用最小假设
并在交付时说明。若选择会改变产品行为、安全、外部状态、兼容性、成本或已确认范围，
必须停止并请求用户决定。

## 通过仓库 Skill 路由工作

仓库工作流位于 `.agents/skills/`。执行前完整阅读所选 `SKILL.md`，遵守其中的
checkpoint，并只使用覆盖任务所需的最小 Skill 集合。

- `$define-requirement`：澄清不完整或有歧义的需求，产出可供确认的目标、干系人、
  MVP、不做项、文件范围、验收标准和风险。
- `$bootstrap-repository`：建立或刷新仓库级 agent 规则、薄适配器、校验器和基线工作流。
- `$deliver-change`：从需求接收、实现、指南同步、验证到交付，编排一次完整的已确认改动。
- `$implement-change`：检查、实现、测试和审计已经批准的代码或文档改动，但不负责发布。
- `$sync-project-guide`：实现后判断 `AGENTS.md`、嵌套指南、`AGENTS.zh-CN.md`
  或 `README.md` 是否必须更新。
- `$publish-change`：执行另行授权的暂存、提交、推送或 PR 发布。实现授权不能被推断为
  发布授权。

执行前对工作分级：

- L0：只读检查、解释、聚焦测试，或不影响接口、依赖、schema、权限和架构的明显
  局部小 bug。可直接在本指南约束下执行。
- L1：影响局部的有界实现或文档改动。小型单一表面修改使用 `implement-change`；完整
  功能、重构、多文件交付或端到端交付使用 `deliver-change`。
- L2：架构、协议、权限、schema、依赖、公共 API、删除、批量重写或仓库基线工作。
  必要时先用 `define-requirement`，得到确认后使用 `deliver-change` 和相关专项工作流。

只有仓库基线工作使用 `bootstrap-repository`。实现改变架构事实、运行命令、验证要求
或公开行为时必须使用 `sync-project-guide`。只有得到单独、明确的发布授权后才能使用
`publish-change`。

## 仓库定位

ZLAgent（`Kkkirito-123/ZLAgent`）是一个以 IM 为主要入口的个人助手产品。它从个人
微信、企业微信群机器人、Webhook、HTTP 和定时任务接收消息，为每轮对话注入技能、
记忆和知识，让 LLM 在受限工具范围内做选择，并通过原始投递渠道返回结果。

它的工程目标是在真实个人助手产品中学习和验证 Agent Harness 实践：能力发现、宿主
授权、执行、结构化失败、证据、副作用与 trace 必须形成一条可检查链路。Hermes Agent
和 OpenClaw 是可扩展个人助手的产品与工程参考，不是要照抄的功能清单或代码来源。

本仓库不是 OpenZLAgent。OpenZLAgent 是独立的通用 Harness 项目，只能作为只读的
工程参考。不要把它的产品结构或代码整体复制到 ZLAgent，也不要声称 ZLAgent 已经
实现 OpenZLAgent 的持久化任务生命周期。

默认工程方向：

- 先建立所有权和依赖边界，再增加行为；
- 复杂任务设置明确 checkpoint 和验收标准；
- 把模型判断与宿主授予的权限分开；
- 使用证据证明完成，不依赖自然语言成功声明；
- 只在符合本产品时参考 OpenZLAgent、DeerFlow、OpenGUI 等成熟开源 agent 实践。

## 主流程与依赖方向

```text
FastAPI lifespan -> bootstrap 组合根
gateway / API / cron -> AgentLoop
AgentLoop -> turn 准备 -> 工具循环 -> turn 后处理
工具循环 / 确认流 / 运维入口 -> HarnessExecution
HarnessExecution -> 具备权限信息的 ToolRegistry -> 内置或 MCP 工具
memory / skills / wiki / graph / storage -> 个人助手持久化状态
```

所有权规则：

- `backend/app.py` 只负责进程生命周期；对象构造和装配属于 `backend/bootstrap/`。
- `backend/gateways/` 负责统一入站消息和出站投递，不得执行工具、授予权限或判断
  任务完成。
- `backend/agent/` 负责一轮对话、模型与工具迭代、确认挂起、上下文准备和 turn 后策略。
- `backend/harness/` 负责显式的单次调用执行边界、可观测性、进度报告和扩展管理边界，
  不得导入 gateway 或 FastAPI 代码。
- `backend/tools/` 负责工具契约、注册表、权限元数据和内置能力。除作为 harness 原始
  调用边界的 `ToolRegistry` 外，禁止绕过 `HarnessExecution` 执行工具。
- `backend/memory/`、`backend/skills/`、`backend/wiki/`、`backend/graph/`、
  `backend/mcp/`、`backend/cron/` 和 `backend/domains/` 是产品能力，不要只为了
  目录整齐而搬迁。
- `backend/db/` 和 `backend/storage/` 不得拥有或依赖 agent 编排逻辑。

`HarnessExecution` 是单次工具调用的唯一执行生命周期；`AgentLoop` 仍然负责对话 turn
生命周期。ZLAgent 当前没有 OpenZLAgent 的不可变持久化 `TaskContract`、持久化
plan/event/checkpoint 账本、验收门、worker 生命周期或事务性 outbox。不能只凭 trace
或结构化工具结果声称 exactly-once 或持久化完成。

## 工具与外部动作边界

- safe 工具必须只读。会修改文件、记忆、技能、定时任务、已安装软件、投递目标或
  外部系统的工具必须属于 confirm 等级。
- confirm 级多动作工具里的只读操作，只有通过 `Tool.is_action_read_only()` 才能免确认。
- 受信任后台审查可以使用宿主明确授予的确认；模型输出永远不能自行授权。
- 确认恢复、prefetch、扩展删除、HTTP 工具测试和普通模型工具调用都必须经过
  `HarnessExecution`。
- 每个工具结果都应包含结构化状态、错误、可恢复性、适用时的下一步动作、来源、证据、
  副作用元数据。自然语言 `content` 不是审计证据；trace 元数据由 Harness 可观测路径
  单独记录。
- trace 中的参数必须脱敏。日志、指标、进度视图和运维界面不得决定用户请求是否成功。
- 在安全检查能降低修改风险时，采用 read-before-write。
- 如实记录不确定的外部结果，不盲目重试非幂等副作用，也不声称持久化 exactly-once。

## 运行时、数据与配置事实

- 支持的运行时为 Python 3.11+。
- Docker 启动 `zlagent`、PostgreSQL/pgvector 和 Redis；Neo4j 不是运行时依赖。
- 本地开发可使用配置指定的本地存储路径；Docker 按已提交的 compose 和环境配置使用
  PostgreSQL 与 Redis。修改默认值前先检查 `backend/core/config.py`。
- MCP 是可选能力，可用 `ZLAGENT_MCP_ENABLED=false` 关闭。
- 没有 LLM key 时，服务通过 echo fallback 启动。
- 维护者目前只报告个人微信链路经过完整端到端验证；企业微信群机器人和通用 Webhook
  仍属于部分链路。
- `.env`、`data/`、运行时 workspace、凭据、日志、缓存、本地数据库和生成物不是源码
  权威，不得提交。

## 敏感区域

- `backend/agent/loop.py` 和 `backend/agent/tool_loop/`
- `backend/agent/confirmation/`
- `backend/harness/execution.py`
- `backend/tools/permission.py` 和 `backend/tools/registry.py`
- 包括 `code_execution.py`、`mcp_manage.py`、`send_message.py`、
  `skill_manage.py` 以及文件或知识写入工具在内的修改型工具
- `backend/mcp/installer.py` 与 MCP 生命周期或传输代码
- `backend/gateways/weixin.py` 和 vendored iLink 协议代码
- 数据库迁移、投递目标处理、密钥与认证

协议、权限、schema、provider、依赖、公共 API 和持久化状态改动，都需要已批准的计划
以及迁移或回滚路径。

## 标准命令

```bash
python -m pip install -e '.[dev]'
python3 scripts/test_validate_rules.py
python3 scripts/validate-rules.py
python -m compileall -q backend scripts tests
python -m unittest discover -s tests
ruff check backend scripts tests
ZLAGENT_DEMO_FAST=1 ZLAGENT_MCP_ENABLED=false python scripts/demo_e2e.py
python scripts/demo_capability_lifecycle.py
docker compose config --quiet
docker compose up -d --build
```

针对 MCP 和 GraphRAG 的脚本可能需要子进程、网络、provider 凭据或运行中的服务。
这些检查必须单独报告，不能描述成离线单元测试。

## 保持架构与公开指南同步

每次实现后都要明确给出指南同步分类：

- `GUIDE_UPDATED`：架构、所有权、依赖方向、协议、权限或安全边界、标准命令、验证要求
  或持久化运行事实发生变化，并且对应 `AGENTS.md` 已更新。
- `GUIDE_NO_UPDATE`：上述由指南管理的事实均未改变；必须说明原因。
- `GUIDE_UPDATE_REQUIRED`：实现改变了指南事实，但尚不能更新指南；该状态阻塞完成。

README 需要独立分类：

- `README_UPDATED`：用户可见的安装、行为、支持状态或运行说明发生变化，并已更新
  `README.md`。
- `README_NO_UPDATE`：没有公开事实变化；必须说明原因。
- `README_UPDATE_REQUIRED`：公开行为已改变但文档尚不能更新；该状态阻塞完成。

包所有权、主流程、依赖方向、协议、权限或安全边界、标准命令、验证要求发生变化时，
必须在同一改动中更新本指南，并保持 `AGENTS.zh-CN.md` 语义同步。用户可见的设置或
行为改变时更新 `README.md`。临时任务状态、临时路径、本机细节和未经验证的声明不能
发布为持久化架构事实。

## 基于证据的交付

- 测试投入与风险相称。mock 不能证明真实微信、provider、数据库、MCP 或外部副作用。
- 先运行聚焦检查，再运行与改动范围相符的标准离线测试。
- 完成前检查最终差异中的凭据、私有端点、生成文件、无关改动、兼容性破坏、调试残留
  和空白错误。
- 未得到单独发布授权时，不暂存、提交、推送、创建 PR、合并、发布或部署。

最终中文报告必须说明：

- 改了什么；
- 改了哪些文件；
- 执行了哪些验证、获得了什么证据；
- 发现的问题或 root cause；
- 剩余风险与未验证链路；
- `GUIDE_*` 和 `README_*` 同步状态；
- 值得沉淀的可复用规则。
