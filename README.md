# ZLAgent

面向个人长期使用、支持 Skill 与 MCP 扩展的 IM 优先 AI 助理，也是一个持续学习
Agent Harness 的实践项目。

![ZLAgent preview](photo.png)

📺 [项目演示视频](https://www.bilibili.com/video/BV1bc5S6dEq6/)

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-green)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-336791)
![Redis](https://img.shields.io/badge/Redis-7.x-DC382D)
![MCP](https://img.shields.io/badge/MCP-Optional-black)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED)
![License](https://img.shields.io/badge/License-MIT-informational)
![Version](https://img.shields.io/badge/version-1.2.2-blue)

ZLAgent 接收微信、Webhook、HTTP 和定时任务消息，在每轮对话中按需加载技能、
上下文、长期记忆与知识，让模型在受权限约束的工具范围内完成任务，再通过原渠道回复。

## 当前状态

| 能力 | 状态 |
|---|---|
| 个人微信双向收发 | ✅ 维护者已完成端到端验证 |
| 企业微信群机器人 | ⚠️ 仅出站，代码已提供，尚未完整实测 |
| 通用 Webhook | ⚠️ 适合测试或自集成，生产链路尚未完整实测 |
| Docker 部署 | ✅ `zlagent` + PostgreSQL/pgvector + Redis |
| MCP | 可选，可通过配置关闭 |
| 无 LLM key 启动 | 支持，回复退化为 echo |

ZLAgent 是个人助手产品，不是通用任务 Harness。OpenZLAgent 可以作为 Harness
工程参考，但两者是独立项目。

## 项目定位与学习边界

产品层面，ZLAgent 的目标是让个人通过微信等熟悉入口调用一个可持续扩展的助手：
本地 Skill 保存可复用方法，MCP 接入外部工具，记忆承接长期偏好，定时任务负责主动
工作。Hermes Agent 与 OpenClaw 是产品和工程参考，不是要逐项复刻的功能清单。

工程层面，本项目用真实个人助手需求学习 Agent Harness：把工具发现、权限确认、执行、
结构化错误、来源证据、副作用与 trace 放进同一条可验证链路。当前 Harness 的责任边界
是“一次工具调用”，不是持久化任务调度系统；仓库尚未提供 durable TaskContract、
worker/outbox 或 exactly-once 副作用保证。

这意味着本文或演示可以讲“如何在旧项目里逐步建立 Harness 边界”，但不能把现状包装成
已经完成的通用 Agent 平台。

## 核心能力

- **多入口对话**：个人微信、企业微信群机器人、Webhook、HTTP、定时任务。
- **上下文与记忆**：短期会话上下文、长期偏好与事实、长对话压缩。
- **技能与知识**：安装或创建本地 Skill，按任务加载，并按规则使用 wiki cache、文件
  知识库与图谱快照。
- **受控工具执行**：内置工具与 MCP 工具统一经过权限、确认、错误恢复和 trace。
- **主动任务**：定时任务、消息投递、复盘和知识维护服务。
- **运行检查**：健康检查、组件自检、网关状态、工具与 MCP 管理接口。

## 架构

```text
Weixin / WeCom / Webhook / HTTP / Cron
                    |
                    v
              Gateway / API
                    |
                    v
                 AgentLoop
        turn 准备 -> 模型/工具循环 -> turn 后处理
                    |
                    v
             HarnessExecution
        权限 -> ToolRegistry -> 内置或 MCP 工具
                    |
                    v
      Memory / Skills / Wiki / Graph / Storage
```

关键边界：

- `backend/app.py` 管理 FastAPI 生命周期，运行时装配集中在 `backend/bootstrap/`。
- `backend/gateways/` 只负责消息标准化和投递，不执行工具或决定任务完成。
- `backend/agent/` 管理一轮对话、上下文、模型循环、确认挂起和 turn 后策略。
- `backend/harness/execution.py` 是单次工具调用的统一执行边界。
- `backend/tools/` 定义工具契约、权限、注册表和内置能力。
- `backend/memory/`、`backend/skills/`、`backend/wiki/`、`backend/graph/`
  保存或生成个人助手状态。

工具结果包含结构化状态、错误类型、恢复建议、来源证据与副作用元数据；自然语言
`content` 不能代替审计证据。ZLAgent 当前没有持久化 TaskContract、事务 outbox 或
exactly-once 副作用保证。

详细工程约束见 [AGENTS.md](AGENTS.md)，中文对照见
[AGENTS.zh-CN.md](AGENTS.zh-CN.md)。

## 快速开始

### Docker（推荐）

```bash
git clone https://github.com/Kkkirito-123/ZLAgent.git
cd ZLAgent
cp .env.example .env
```

在 `.env` 中配置 OpenAI 兼容模型：

```env
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=
```

不填写模型配置也能启动，但只能得到 echo 回复。

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://localhost:8020/api/health
```

默认服务：

| 服务 | 地址 |
|---|---|
| ZLAgent | `http://localhost:8020` |
| PostgreSQL/pgvector | `localhost:5432` |
| Redis | `localhost:6379` |

如果宿主机的 `5432` 已被占用，请调整 compose 的 PostgreSQL 端口映射；容器内部连接
仍使用 `postgres:5432`。

修改 `.env` 后需要重新创建容器，单纯 `docker compose restart` 不会刷新容器环境变量：

```bash
docker compose up -d --force-recreate zlagent
```

### 本地开发

本地可以使用 SQLite，并关闭 Redis 与 MCP：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
export ZLAGENT_DATA_DIR="$PWD/data"
export ZLAGENT_CONFIG_DIR="$PWD/config"
export ZLAGENT_WORKSPACE_DIR="$PWD/workspace"
export DATABASE_URL="sqlite:///$PWD/data/zlagent.db"
export ZLAGENT_REDIS_URL=""
export ZLAGENT_MCP_ENABLED=false
python -m uvicorn backend.app:app --reload --port 8020
```

PowerShell 请将 `export NAME=value` 替换为 `$env:NAME="value"`。

## 接入消息渠道

### 个人微信

主服务启动后运行一次扫码工具：

```bash
docker compose run --rm weixin-login
```

该命令会：

1. 在终端显示二维码；
2. 把凭据保存到共享的 `workspace` volume；
3. 热加载微信 gateway；
4. 注册投递目标并发送测试消息。

日志出现 `agent turn: platform=weixin` 和 `[weixin send] ... ok`，表示收发链路已通。
若测试消息返回 `ret=-2`，通常是 iLink 临时限流，等待一至三分钟后重试投递目标测试。

凭据属于运行时敏感数据，不得提交到仓库。

### Webhook

应用启动后可用 HTTP 模拟一条入站消息：

```bash
curl -X POST http://localhost:8020/api/gateways/webhook \
  -H 'Content-Type: application/json' \
  -d '{"platform":"webhook","user_id":"alice","text":"今天天气怎么样"}'
```

Webhook 当前主要用于测试和自定义集成。企业微信群机器人当前仅支持出站投递，使用前
请自行完成真实环境验证。

## 工具、权限与 MCP

工具权限分为三层：

- `safe`：只读，可直接执行；
- `confirm`：会写入、发送、安装或修改外部状态，需要用户或宿主明确授权；
- `deny`：禁止注册和执行。

confirm 级多动作工具只有在 `Tool.is_action_read_only()` 明确判定当前 action 只读时，
才能免确认。普通模型调用、确认恢复、HTTP 工具测试和后台复盘都经过
`HarnessExecution`。

MCP 默认可用，也可以关闭：

```env
ZLAGENT_MCP_ENABLED=false
```

安装外部 MCP、执行代码、写文件、发消息和修改定时任务都属于有副作用操作。模型输出
不能为自己授权。

仓库提供一个完全离线的能力生命周期演示：它先验证 Skill 创建会被确认门拦截，再由
宿主显式授权创建本地 Skill；随后注册一个只读 Mock MCP 工具，通过
`HarnessExecution` 执行并产生 `mcp://` 来源证据，最后从统一生命周期入口卸载 Skill。
该演示不证明真实第三方 MCP transport、网络或 provider：

```bash
python scripts/demo_capability_lifecycle.py
```

## 记忆与知识边界

- **会话上下文**：承接最近对话与补槽信息。
- **长期记忆**：保存稳定偏好、事实和可复用经验。
- **Wiki cache**：只为显式启用缓存的技能复用答案；干净环境为空是正常状态。
- **文件知识库**：由明确的知识写入动作更新。
- **图谱快照**：从已有内容生成视图，不依赖 Neo4j。

“好的”“继续”等低信息量确认词不会自动成为新意图或长期知识。写入型记忆、知识与
技能管理动作必须经过权限边界。

## 常用接口

| 用途 | 接口 |
|---|---|
| 健康检查 | `GET /api/health` |
| 组件自检 | `GET /api/doctor` |
| 运行时概览 | `GET /api/runtime` |
| 网关状态 | `GET /api/gateways` |
| LLM 状态与测试 | `GET /api/llm/status`、`POST /api/llm/test` |
| 工具列表与直测 | `GET /api/tools`、`POST /api/tools/{name}/test` |
| 待确认操作 | `GET /api/confirmations` |
| 记忆与定时任务 | `/api/memory`、`/api/cron` |
| MCP 管理 | `/api/mcp/servers`、`/api/mcp/tools` |
| 知识与图谱 | `/api/knowledge-bases`、`/api/graph-rag` |

完整路由以 `backend/api/` 和运行时 OpenAPI 文档为准。

## 配置

常用配置均在 `.env.example` 中提供。完整字段和默认值以
`backend/core/config.py` 为准。

```env
# LLM
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=

# 可选路由模型
ZLAGENT_ROUTER_LLM_ENABLED=false
ZLAGENT_ROUTER_LLM_MODEL=

# 工具与上下文
ZLAGENT_TOOL_GUARDRAILS_HARD_STOP_ENABLED=true
ZLAGENT_CONTEXT_SUMMARY_ENABLED=true

# 可选能力
ZLAGENT_MCP_ENABLED=true
TAVILY_API_KEY=
ZLAGENT_SEARXNG_URL=

# Docker 默认后端
DATABASE_URL=postgresql+psycopg://zlagent:zlagent@postgres:5432/zlagent
ZLAGENT_REDIS_URL=redis://redis:6379/0
```

`.env`、`data/`、`workspace` 运行时状态、凭据、日志和本地数据库不得提交。

## 验证与质量

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
```

GitHub Actions 在 Python 3.11 与 3.13 上运行规则检查、编译、单元测试、ruff、离线
产品 demo 和离线 Harness 能力生命周期 demo。以下脚本需要网络、provider 凭据、
外部进程或运行中的服务，结果应单独报告：

```bash
python scripts/smoke_llm_graph.py
python scripts/mcp_e2e_check.py
python scripts/mcp_e2e_http_check.py
```

## 代码阅读路径

| 目标 | 入口 |
|---|---|
| 启动与装配 | `backend/app.py`、`backend/bootstrap/` |
| 一轮对话 | `backend/agent/loop.py`、`backend/agent/turn_preparer/` |
| 工具执行与权限 | `backend/harness/execution.py`、`backend/tools/` |
| 确认恢复 | `backend/agent/confirmation/` |
| MCP | `backend/mcp/` |
| 记忆与知识 | `backend/memory/`、`backend/wiki/`、`backend/graph/` |
| 微信 | `backend/gateways/weixin.py`、`backend/cli/weixin_login.py` |

## License

本项目以 [MIT License](LICENSE) 发布；保留、改写和参考来源见
[ATTRIBUTIONS.md](ATTRIBUTIONS.md)。
