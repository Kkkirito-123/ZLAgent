# ZLAgent

面向个人日常使用的 IM 优先 AI 助理平台。

![ZLAgent preview](photo.png)

📺 **项目演示视频**：[bilibili.com/video/BV1bc5S6dEq6](https://www.bilibili.com/video/BV1bc5S6dEq6/)

项目基于 `FastAPI`、`PostgreSQL + pgvector`、`Redis`、`MCP` 和 OpenAI 兼容大模型构建。系统采用 **先识别意图与技能，再注入记忆与上下文，再调用工具执行，最后按代码规则写入记忆、wiki cache 或文件型知识库** 的流程，可在微信 / Webhook 等 IM 场景下完成论文查询、旅游规划、日报订阅、定时任务、记忆管理、知识库写入和工具扩展。

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-green)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-336791)
![Redis](https://img.shields.io/badge/Redis-7.x-DC382D)
![MCP](https://img.shields.io/badge/MCP-Tools-black)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED)
![License](https://img.shields.io/badge/License-MIT-informational)
![Version](https://img.shields.io/badge/version-1.2.2-blue)

---

## v1.2.2 与 origin 第一版的差异

origin 第一版已经具备对话、工具调用、记忆、知识沉淀、定时任务和微信接入等基础能力。当前版本主要调整方向是：删除未落地的依赖和能力描述，明确已验证链路，并把对话上下文、长期记忆、工具权限、失败处理和运行检查整理为更清晰的服务架构。

### 架构边界差异

| 维度 | origin 第一版 | 当前版本 |
|---|---|---|
| 运行依赖 | README 和 compose 中包含 Neo4j，并把图数据库作为 GraphRAG 的落地目标 | 移除 Neo4j 运行依赖，默认服务为 `zlagent`、`postgres`、`redis` |
| 消息渠道 | README 描述微信、飞书、Webhook，并保留 Email、Telegram 等方向 | README 只把个人微信标为已端到端验证；WeCom Bot、Webhook 标注为代码面或测试面 |
| 启动流程 | 初始化、接口、后台任务和消息通道的说明集中在一起 | 启动、接口注册、后台任务、消息通道分别说明，部署边界更明确 |
| 工具调用 | 更强调工具清单和文件结构 | 更强调统一调用流程、权限确认、失败处理和运行记录 |
| 记忆与知识 | 长期记忆、wiki、文件知识库、图谱容易被理解为自动连续沉淀链路 | 明确区分短期上下文、长期记忆、wiki 缓存、文件知识库和图谱视图，各自有触发条件 |
| 图谱能力 | GraphRAG 被描述为写入 Neo4j 的知识图谱 | 当前从已有记录生成图谱视图或缓存，不再写入 Neo4j |
| 验证状态 | 主要描述设计和脚本验证 | 补充 Docker 三服务、微信收发、HTTP 接口、MCP、GraphRAG 接口的实测状态 |

### 核心机制差异

| 机制 | origin 第一版的表达 | 当前版本的差异 |
|---|---|---|
| 主流程 | 已有“收到消息 -> 调模型 -> 用工具 -> 回复”的基本链路 | 各入口统一进入同一套对话流程，减少重复业务分支 |
| 短期记忆 | 已有对话上下文能力，但与长期记忆说明混合 | 最近对话单独作为短期上下文，用于承接省略信息和补充参数 |
| 长期记忆 | 已有记忆系统，但写入边界不够突出 | 只保存偏好、事实、经验等可复用信息，避免把低信息量回复写入长期记忆 |
| 历史压缩 | 已有压缩能力，说明更偏实现细节 | 长对话优先保留最近内容，并压缩旧工具结果和中间过程 |
| 缓存 | Redis、wiki、工具缓存分散说明 | 明确为对话热缓存、重复查询缓存、技能答案缓存三类 |
| 错误处理 | 已有失败恢复能力，但不是架构主线 | 工具失败会回填给模型；重复失败会被拦截；常见失败会记录为后续参考 |
| 超时与重试 | 各模块存在 timeout / retry | 明确模型路由、定时任务发送、微信发送等链路的超时和重试边界 |
| 权限确认 | 已有安全等级和确认机制 | 安全动作直接执行，高风险动作需要确认，不允许的动作直接拒绝 |
| 知识写入 | 容易被理解为所有内容都会自动沉淀 | 记忆、wiki、文件知识库、图谱视图均按触发条件写入或生成 |
| 运行检查 | 有部分检查接口 | README 明确列出健康检查、运行状态、网关状态和工具调用统计 |

当前版本的主要变化不是增加功能数量，而是明确架构边界：哪些依赖已经移除，哪些通道已经验证，哪些状态会被保存，哪些能力需要确认，以及失败、超时和重试如何处理。

参考 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的部分思路主要体现在工程组织方式上：模型负责判断，代码负责对话流程、工具调用、记忆、权限和失败处理；ZLAgent 将这些思路改造到 IM 常驻助理场景。

---

## 设计思路：对话优先

ZLAgent 的交互入口以 IM 对话为主。用户通过微信、Webhook 或测试接口提交自然语言请求，系统在后台处理上下文、记忆、工具、权限和消息发送。

```text
        ┌──────────────────────────────────────────┐
        │   用户看到的部分：微信 / Webhook 对话      │
        │  ┌────────────────────────────────────┐  │
        │  │   系统处理的部分：工具、权限、任务    │  │
        │  │  ┌──────────────────────────────┐  │  │
        │  │  │   底层保存的部分：记忆、知识、   │  │  │
        │  │  │   缓存、数据库                 │  │  │
        │  │  └──────────────────────────────┘  │  │
        │  └────────────────────────────────────┘  │
        └──────────────────────────────────────────┘
```

| 层次 | 做什么 | 用户是否需要关心 |
|---|---|---|
| 对话层 | 微信、Webhook、测试接口 | 需要，作为主要交互入口 |
| 执行层 | 选工具、确认危险动作、运行任务 | 通常不需要，涉及高风险动作时需要确认 |
| 记忆层 | 保存偏好、知识、缓存和图谱视图 | 不需要 |
| 基础层 | 模型接口、数据库、Redis | 不需要 |

**使用方式**：

1. **接入能力**：用户提出需要的能力，系统检查是否可以接入，必要时要求确认。
2. **日常使用**：用户继续通过自然语言对话，系统选择工具、带入记忆并处理结果。

记忆怎么存、技能怎么选、图谱怎么抽、上下文怎么压缩、失败怎么恢复——**用户全程不需要知道**。

---

## 核心数据流

```text
用户消息（微信 / WeCom Bot / Webhook）
        |
        v
识别这句话要做什么
判断是新任务，还是接着上一轮说
        |
        v
带上必要的上下文和长期记忆
        |
        v
只加载当前任务需要的技能和知识
        |
        v
模型决定下一步
需要工具就调用工具，需要确认就问用户
        |
        v
答案缓存  →  文件知识库  →  图谱视图
答案缓存 / 文件知识库 / 图谱快照三层复用
        |
        v
回复之后再做后台整理
按规则写记忆、写缓存或更新知识库
```

核心思路：

> **先理解用户要什么，再带上必要记忆；先收窄可用能力，再调用模型；能复用的信息再按规则保存。**

## 路由与写入安全边界

为了避免上下文污染，ZLAgent 对“确认词”和“补槽词”做了区分：

- 纯确认词，如 `可以`、`好的`、`嗯`、`行`、`继续`
  - 不应被当作新意图
  - 不应触发新的技能继承
  - 不应写入 wiki 或长期记忆

- 真正的补槽短句，如 `三天吧`、`预算 2000`、`从上海出发`
  - 可以继承上一轮有效上下文
  - 可以作为当前技能的参数补全

系统在路由层、写入层、知识缓存层都设置了防污染检查，避免把低信息量回复误写成可复用知识。

---

## 架构参考：模型负责判断，代码负责执行

这个项目参考了 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的思路：模型本身负责判断、推理和生成；代码负责把消息、上下文、记忆、工具、权限、后台任务组织好。也就是说，ZLAgent 不在代码里写死复杂决策，而是给模型准备一个稳定的工作环境。

可以把 ZLAgent 理解成：

```text
ZLAgent = 模型
        + 微信 / Webhook 入口
        + 一条稳定的对话流程
        + 上下文和记忆
        + 工具和 MCP 扩展
        + 权限确认
        + 知识缓存和文件知识库
        + 运行检查
```

其中最核心的循环只有一个：

```text
用户消息
  -> 组装上下文
  -> 模型判断下一步
  -> 如果需要工具：执行、回填结果、继续循环
  -> 如果不需要工具：生成回复
  -> 回复 IM
  -> 后台按规则写入记忆或 wiki cache
```

这个主流程不因为新增工具、接入微信、增加知识库而改变。新增能力只需要接进同一套流程。

## 当前架构

### 1. 入口层：把真实世界变成标准消息

用户不直接面对 API 和数据库，而是在微信、Webhook 或运维接口里发自然语言。入口层只做三件事：

- 把不同渠道的消息统一成 `IncomingMessage`
- 给回复准备好对应的 `DeliveryTarget`
- 记录网关收发状态，便于排查“收到了没有、发出去了没有”

这层的目标是让后面的主流程不关心消息来自微信还是 Webhook。

### 2. 对话准备：每一轮开始前先整理

每轮对话开始前，系统会先整理当前用户、会话、上一轮上下文、可用技能、长期记忆、工具权限和追踪信息。

它负责回答几个问题：

- 这句话是新任务，还是上一轮的补槽？
- 需要加载哪个技能，而不是把所有技能都塞进提示词？
- 哪些长期记忆应该带上，哪些不该污染当前对话？
- 模型是否可以直接回复，还是要调用工具？

### 3. 主流程：模型决策，代码执行

ZLAgent 的代码不替模型做复杂决策。代码只维护循环：

- 模型要回复，就把回复送回 IM
- 模型要查资料、读文件、建任务、发消息，就走工具执行
- 工具结果再回填给模型，让模型继续判断
- 高风险动作进入确认流，用户在 IM 里回复同意后再执行

工具不是独立的业务分支，而是统一执行流程的一部分。所有工具都走同一套调用、权限、失败处理和日志记录流程。

### 4. 工具管理：内置能力 + MCP 扩展

工具分两类：

- **内置能力**：记忆、定时任务、知识写入、文件读写、网页搜索、消息投递等平台基础能力
- **外部能力**：通过 MCP 动态接入第三方工具，并统一进入同一个工具池

用户的体验不应该是“记住工具名”，而是说出需求：例如“我需要查实时航班”“每天早上推送论文摘要”。系统再判断已有能力是否够用，不够再走 MCP 安装和确认。

### 5. 记忆和知识：把可复用信息留下来

ZLAgent 的长期价值不在单次回答，而在代码明确允许的状态写入：

- **长期记忆** 保存用户偏好、长期事实和使用习惯
- **答案缓存** 保存明确开启缓存的技能回答，下一次可以直接复用
- **文件知识库** 把用户确认保存的主题资料写成 markdown 文件
- **图谱视图** 从已有记录临时整理节点和关系，供接口和页面查看

这几层的关系是：记忆和答案缓存是可写入状态，文件知识库保存成 markdown，图谱视图从这些已有内容里整理出来。

### 6. 运行检查：让它能长期在线

因为目标是 IM 常驻助理，系统必须能被监控和恢复：

- Docker Compose 管理 `zlagent + postgres + redis`
- `/api/health` 看活性
- `/api/doctor` 看关键子系统
- `/api/runtime` 看当前运行状态
- `/api/gateways` 看 IM 网关状态
- `/api/harness/metrics` 看工具调用和运行统计

这一层用于支持长期运行和故障定位。

---

## 技能、记忆与知识写入流程

ZLAgent 当前代码里的写入路径分为三类：

1. **长期记忆**
   - 用户偏好、固定习惯、可复用事实
   - 由 `memory` 子系统管理，持久化到 PostgreSQL

2. **答案缓存**
   - 高频问答、稳定答案、可复用事实
   - 由 `wiki` 子系统管理；只有 skill manifest 里启用 `wiki_cache` 时才会自动写入
   - skill manifest 里启用 `crystallize` 时，会额外调用 crystallizer 拆出 atomic facts

3. **文件型知识库**
   - `knowledge_mode_manage` 创建 `workspace/knowledge_modes/<mode_id>/`
   - `knowledge_ingest` 写入 `wiki/outputs/*.md`，并更新 `wiki/index.md` 与 `wiki/log.md`

### 关于技能管理

系统可以辅助生成、审查和维护技能，但不会在没有控制的情况下随意“自动长出”新技能文件。  
技能通常通过以下方式产生：

- 人工编写 `workspace/skills/<name>/SKILL.md`
- 通过 `skill_manage` / `curator` 流程创建或更新
- 经 review / 审查后进入可用状态

也就是说，ZLAgent 会在规则允许时写入记忆、wiki cache 或知识库文件，但技能本身仍然是受控演进的。

## 适用场景与边界

ZLAgent 适合以下场景：

- 个人 IM 日常助理
- 论文 / 旅行 / 天气 / 记忆 / 定时任务等高频对话任务
- 需要长期记忆、技能管理、MCP 工具扩展的助理系统
- 需要在微信 / Webhook 里直接完成自然语言交互的场景

ZLAgent 不适合以下场景：

- 强实时交易系统或高频低延迟业务核心
- 必须强事务一致性的在线业务主系统
- 超大规模多人协作知识库的完整替代品
- 需要完全无审核自动写入生产技能的场景

---

## 架构概览

```mermaid
flowchart TD
    A[用户消息: 微信 / WeCom Bot / Webhook] --> B[消息入口]
    B --> C[后端接口]
    C --> D[主对话流程]

    D --> E[判断用户要做什么]
    E --> F{选择需要的技能或知识库}
    F --> G[带上相关记忆]
    G --> H[加载当前需要的技能]
    H --> I[调用模型]

    I --> J1[内置工具]
    I --> J2[MCP 外部工具]
    I --> J3[是否命中答案缓存]

    J3 -->|命中| K[直接复用缓存答案]
    J3 -->|未命中| L[继续让模型处理]

    J1 --> M[权限判断]
    J2 --> M
    M -->|安全| N[执行]
    M -->|需要确认| O[等待用户确认]
    O -->|用户在 IM 里同意| N
    N --> P[工具结果]
    P --> I

    I --> Q[回复到 IM]
    Q --> R[回复后的后台整理]
    R --> S1[写入长期记忆]
    R --> S2[可选写入答案缓存]
    R --> S3[可选做技能复盘]

    X[文件知识库] --> Y[图谱视图接口]
    S1 --> Y
```

---

## 记忆、工具与权限如何协作

ZLAgent 的重点不是“有多少工具”，而是每一轮对话里这些能力如何被安全地交给模型。

### 记忆进入上下文

记忆不是一股脑塞给模型。每轮对话会先判断当前消息是否需要长期事实，再取少量相关记忆带进去。用户说“以后默认坐高铁”这类长期偏好时，会在回复之后进入后台整理，再写入长期记忆。

这样做有两个目的：

- 让模型记得用户偏好，而不是每次从零开始
- 避免把无意义确认词、临时补槽、一次性噪声写成长期事实

### 工具怎么接进来

工具统一接到同一套调用流程里。模型只需要知道“当前可以做什么”，不需要关心底层是内置函数、MCP server，还是某个领域包。

工具调用的统一规则是：

- 只读能力可以直接执行
- 写入、发送、安装、执行代码等高风险动作进入确认流
- 重复失败、无进展调用会被失败保护拦住
- MCP 接入的外部能力必须先经过安装参数校验、运行时注册和权限分配

这让系统可以扩展能力，但不会因为扩展而失去边界。

### 权限不是弹窗，而是对话协议

ZLAgent 面向 IM，所以确认也发生在 IM 里。模型提出动作，系统生成可读确认消息，用户回复“可以 / 不行”，然后系统继续执行或取消刚才那个动作。确认不是 Web 控制台上的按钮，而是对话的一部分。

### MCP 的位置

MCP 不是第二套 agent，也不是绕过权限的插件系统。它只是外部工具的一种接入方式：

```text
内置能力
外部 MCP 能力
领域能力
        |
        v
统一工具注册表
        |
        v
权限判断 / 确认 / 执行 / 观测
        |
        v
主对话流程
```

所以后续加新能力时，不需要改主流程，只需要把能力接入同一套注册、权限和日志体系。

---

## 外部信息与知识写入

ZLAgent 把“临时查资料”和“明确写入知识库”分开处理。

### 临时信息：查完即用

网页搜索、实时交通、天气、论文检索这类能力属于临时信息层。它们的共同特点是：

- 结果有时效性，不应该默认写成长期记忆
- provider 可以切换，模型不需要知道底层来源
- 工具输出会归一成稳定格式，方便模型继续推理
- 查不到、超时、限流时返回可解释错误，而不是静默失败

这类能力主要解决“当前问题需要外部事实”的场景。

### 长期知识：可复用、可阅读、可查询

当前代码里的长期知识分三层：

| 层 | 作用 | 适合存什么 |
|---|---|---|
| 答案缓存 | 让高频稳定问答直接复用 | 旅行路线、常见解释、固定流程 |
| 文件知识库 | 让保存结果可读、可改、可审查 | 论文摘要、项目资料、专题知识 |
| GraphRAG 快照 | 让已有记录以节点 / 边形式查看 | 人、项目、偏好、论文、主题之间的关系 |

GraphRAG 不在同步对话链路里阻塞用户。当前 `/api/graph-rag` 和 `/api/knowledge-bases/{id}/graph` 会从 memory、knowledge mode 文件、manual/runtime records 构建快照；`/api/knowledge-bases/{id}/rebuild-graph` 可触发 LLM 抽取并写入 graph cache。

### 为什么要分三层

单一“向量库”很容易混掉不同用途的信息。ZLAgent 分三层是为了让每类知识有清晰边界：

- 答案缓存追求快
- 文件知识库追求可读和可维护
- 图谱快照追求结构化关系

这也是系统从“会回答一次”走向“越用越懂你”的关键。

---

## 知识与缓存三层

| 层 | 模块 | 内容 | 命中速度 |
|---|---|---|---|
| 答案缓存 | `backend/wiki/store.py` + `normalizer.py` + `crystallizer.py` | `(skill_id, normalized_query) → answer + TTL`；近义合并归一为 canonical key | 毫秒级 |
| 文件知识库 | `workspace/knowledge_modes/<mode>/wiki/` + `knowledge_ingest` 工具 | 写入页 markdown + `index.md` + `log.md`，按 mode 分库 | 文件读 |
| 图谱快照 | `backend/graph/` + graph cache / JSON snapshot | LLM 抽取或启发式构建的结构化关系网络 | 文件 / 内存快照 |

三层互相独立：某 skill 可只用答案缓存；某 mode 可只产 markdown 文件；GraphRAG API 再从已有记录构建快照。

---

## 领域能力：以旅行为例

旅行是一个典型的高频领域：它既需要用户偏好，又需要实时信息，还会把可复用的旅行回答写入 wiki cache。

ZLAgent 把旅行能力做成领域包，而不是只靠普通提示词：

- 用户偏好来自 memory，例如“默认坐高铁”
- 实时查询来自工具，例如 12306 车次和 Open-Meteo 天气；航班、酒店等需要接入对应 MCP
- 常见路线可以进入答案缓存
- 去过的城市可以进入 visited map
- 复杂计划仍然交给模型组织成自然语言

这类领域包的意义是：把稳定业务能力做成可复用模块，把个性化判断留给模型。

---

## 技术栈

| 类型 | 技术 |
|---|---|
| Web 服务 | FastAPI + Uvicorn |
| 对话流程 | 自研主流程 + 工具注册 + 权限确认 + 运行检查 |
| LLM | OpenAI 兼容（DeepSeek / Qwen / OpenAI 等） |
| 关系数据库 | PostgreSQL 16 + pgvector |
| 缓存 / Session | Redis 7（LRU 256MB） |
| 工具协议 | MCP（stdio + streamable-http） |
| 包管理 | npm / pip / uvx / git+ |
| 长期记忆 | PostgreSQL `UserMemory` 表 + JSONL 会话归档 |
| 知识库 | 文件型 markdown + 图谱视图 / 模型抽取缓存 |
| 运行环境 | Python 3.11+ / Docker / macOS / Linux / Windows PowerShell |

---

## 快速开始

### 1. 克隆项目

```bash
git clone <your-zlagent-repo-url>
cd ZLAgent
```

### 2. 配置环境变量

```bash
cp .env.example .env
$EDITOR .env
```

至少需要配置：

```env
OPENAI_API_KEY=your-llm-api-key
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-pro
```

可选：

```env
ZLAGENT_ROUTER_LLM_ENABLED=true
ZLAGENT_ROUTER_LLM_MODEL=deepseek-v4-flash

WEIXIN_BASE_URL=http://...
```

### 3. Docker 一键启动（推荐）

```bash
docker compose up -d --build
```

如果本机已经有 Postgres 占用了 `5432`，把 ZLAgent 的宿主机映射改到 `15432`，容器内部仍然走 `postgres:5432`：

```bash
docker compose -f docker-compose.yml -f <(printf 'services:\n  postgres:\n    ports: !override\n      - "15432:5432"\n') up -d --build
```

会拉起三个服务：

```text
zlagent           http://localhost:8020
postgres+pgvector localhost:5432  或  localhost:15432
redis             localhost:6379
```

### 4. 验证

```bash
docker compose ps
/usr/bin/curl -sS http://localhost:8020/api/health
/usr/bin/curl -sS http://localhost:8020/api/llm/status
/usr/bin/curl -sS -H 'Content-Type: application/json' \
  -d '{"text":"只回复 ok"}' \
  http://localhost:8020/api/llm/test
```

Docker 运行镜像默认只复制 `backend/`、`workspace/` 和入口脚本；如果要在容器里跑仓库测试脚本，先临时复制 `scripts/`：

```bash
docker compose exec -T -u root zlagent sh -lc 'rm -rf /app/scripts && mkdir -p /app/scripts'
docker cp scripts/. zlagent:/app/scripts/
docker compose exec -T -u root zlagent sh -lc 'chown -R zlagent:zlagent /app/scripts && chmod -R u=rwX,go=rX /app/scripts'

docker compose exec -T zlagent python -m compileall -q backend scripts
docker compose exec -T zlagent python scripts/smoke_llm_graph.py
docker compose exec -T zlagent env ZLAGENT_DEMO_FAST=1 python scripts/demo_e2e.py
docker compose exec -T zlagent python scripts/mcp_e2e_check.py
docker compose exec -T zlagent python scripts/mcp_e2e_http_check.py
```

健康检查：`GET http://localhost:8020/api/health` → `{"status":"ok","version":"1.2.2"}`

八组件自检：`GET http://localhost:8020/api/doctor`

### 5. 日常监控

```bash
while true; do
  clear
  date
  echo
  docker compose ps
  echo
  /usr/bin/curl -fsS http://localhost:8020/api/health || echo "health check failed"
  echo
  echo "---- latest zlagent logs ----"
  docker compose logs --tail=40 zlagent
  sleep 5
done
```

只看实时日志：

```bash
docker compose logs -f --tail=100 zlagent
```

改 `.env` 后不要只 `restart`，因为容器环境变量不会刷新。需要 recreate 主容器：

```bash
docker compose -f docker-compose.yml -f <(printf 'services:\n  postgres:\n    ports: !override\n      - "15432:5432"\n') up -d --force-recreate zlagent
```

---

## 使用示例

### 论文查询

```text
帮我找几篇最近关于 LLM 长上下文的顶会论文
```

系统命中 `paper-search` skill，调用 arxiv + Semantic Scholar，必要时用 `web_search` 兜底，排序去重后回 IM。需要保存时，再由 `knowledge_ingest` 写入指定 knowledge mode。

### 旅行规划

```text
帮我规划下周五去成都三天的行程，预算 3000
```

进入 `domains/travel/`：行程编排 → 12306 实时余票 → 路线优化；若已查过相似问题，命中 wiki 缓存直接返回。

### 主动记住偏好

```text
我以后默认坐高铁不坐飞机
```

Intent Detector 识别 durable instruction，触发 background review fork 写入 `user_fact`。下次规划行程时，memory 注入会把这个偏好带回上下文。

### 安装新工具

```text
我需要一个查实时航班的工具
```

Agent 检测能力缺口，从 MCP 注册表搜索候选；用户确认后走 `install_and_add` 一次完成安装与注册；可选 `promote` 把只读工具提到 safe，免后续确认。

### 定时任务

```text
每天早上 8 点把昨晚的 arxiv ai 论文整理推给我
```

`cron_manage(create)` 写入定时任务，到点跑 skill，结果统一推送 IM。

### 知识库写入

```text
记一下：项目 X 的截止日是 6 月 30 日，负责人是张三
```

`knowledge_ingest` 写入指定 knowledge mode 的 markdown 页，并更新 `wiki/index.md` 与 `wiki/log.md`。之后 GraphRAG 快照接口可以从这些文件构建节点和边。

---

## 能力分层说明

ZLAgent 中不同概念的职责如下：

- **skills**
  - 行为模板和任务范式
  - 决定“怎么回答 / 怎么执行”

- **memory**
  - 用户偏好、历史上下文、长期事实
  - 决定“记住什么”

- **wiki**
  - 可复用答案缓存
  - 决定“什么可以直接复用”

- **knowledge_modes**
  - 面向特定主题或场景的知识库
  - 决定“某类知识如何组织”

- **graph**
  - 从记忆和知识中抽取关系网络
  - 决定“知识之间如何关联”

---

## 接入 IM 渠道

| 平台 | 标识 (`platform`) | 方向 | 状态 |
|---|---|---|---|
| 个人微信 | `weixin` | 双向 | ✅ **作者已端到端验证过**，下面给出完整步骤 |
| 企业微信群机器人 | `wecom_bot` | 仅出 | ⚠️ 代码与接口已提供，**未实测**，请自行调试 |
| 通用 Webhook | `webhook` | 入站测试 / 日志出站 | ⚠️ 代码与接口已提供，**未实测生产链路**，请自行调试 |

### 1. 个人微信（Weixin）

**实现原理**：vendored 自 Hermes Agent 的 iLink Bot 协议，扫码后用拿到的 token 长轮询拉消息。

#### 一键搞定（推荐）

**前提**：ZLAgent 主服务已经在跑。

```bash
docker compose -f docker-compose.yml -f <(printf 'services:\n  postgres:\n    ports: !override\n      - "15432:5432"\n') run --rm weixin-login
```

`weixin-login` 是 `docker-compose.yml` 里专门为扫码注册场景准备的一次性服务，挂 `setup` profile（默认 `up` 不启动），复用 zlagent 的 image 和 `zlagent_workspace` named volume，所以凭据直接落到主容器能看到的位置。

CLI 会自动跑完整套流程：

1. 终端打印二维码 → 用手机微信扫
2. 凭据保存到 `workspace/credentials/weixin/accounts/<account_id>.json`（chmod 600）
3. 自动 `POST /api/gateways/weixin/reload` 让 gateway 热加载新凭据
4. 自动 `POST /api/delivery-targets` 注册一条 `platform=weixin` 的投递目标
5. 自动 `POST /api/delivery-targets/{id}/test` 给你的微信发一条测试消息

看到 `✓ 全部完成。请在微信里查收来自 iLink bot 的测试消息。` 就成了，打开微信能看到 `[ZLAgent] test message for '我的微信'`。

如果最后一步测试消息超时，但前两步已经显示：

```text
[1/3] 热加载 weixin gateway 凭据... ✓ OK
[2/3] 注册 DeliveryTarget ... ✓ OK
```

通常表示微信已经连上，只是 iLink 侧临时限流。日志里会看到 `ret=-2` 和 `rate limited`。等 1-3 分钟后手动重发即可：

```bash
/usr/bin/curl -sS -X POST http://localhost:8020/api/delivery-targets/1/test
```

只要日志里出现 `agent turn: platform=weixin` 和 `[weixin send] ... ok`，就说明微信收发链路已经通了。

#### 可选参数

通过 `docker compose run` 透传：

```bash
# 自定义投递目标显示名
docker compose -f docker-compose.yml -f <(printf 'services:\n  postgres:\n    ports: !override\n      - "15432:5432"\n') run --rm weixin-login \
  python -m backend.cli.weixin_login \
  --api-base http://zlagent:8020 --display-name "工作微信"

# 只扫码、不自动注册
docker compose -f docker-compose.yml -f <(printf 'services:\n  postgres:\n    ports: !override\n      - "15432:5432"\n') run --rm weixin-login \
  python -m backend.cli.weixin_login \
  --api-base http://zlagent:8020 --no-auto-register
```

#### 手动流程（仅 `--no-auto-register` 时需要）

`--no-auto-register` 跑完后 CLI 会打印手动命令，照着粘即可。或参考下面：

<details>
<summary>展开手动 curl 命令</summary>

```bash
# 1. 注册 DeliveryTarget
/usr/bin/curl -sS -X POST http://localhost:8020/api/delivery-targets \
  -H 'Content-Type: application/json' \
  -d '{"platform":"weixin","target_type":"user","target_id":"<CLI 打印的 user_id>","display_name":"我的微信"}'

# 2. 热加载
/usr/bin/curl -sS -X POST http://localhost:8020/api/gateways/weixin/reload

# 3. 发测试消息（id 用上一步返回的）
/usr/bin/curl -sS -X POST http://localhost:8020/api/delivery-targets/1/test
```

</details>

### 2. 企业微信群机器人（WeCom Bot） ⚠️ 未实测

> 接口与代码路径都已铺好（`backend/gateways/wecom_bot.py`），作者本人未走通端到端流程。下面是按代码读出来的预期用法，**请自行调试**，遇到问题欢迎反馈。

**仅 outbound**——不能接收群消息，只能往群里推。适合 cron 告警 / 知识库更新提醒。

**步骤**：

1. 群管理员添加群机器人，复制 webhook URL，形如：
   ```text
   https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<uuid>
   ```

2. 注册 DeliveryTarget，`target_id` 直接填完整 webhook URL：

   ```powershell
   $body = @{
       platform     = "wecom_bot"
       target_type  = "webhook"
       target_id    = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
       display_name = "运维告警群"
   } | ConvertTo-Json

   Invoke-RestMethod -Method Post `
     -Uri http://localhost:8020/api/delivery-targets `
     -ContentType "application/json" -Body $body
   ```

3. 测试：

   ```powershell
   Invoke-RestMethod -Method Post -Uri http://localhost:8020/api/delivery-targets/2/test
   ```

   群里看到 `[ZLAgent] test message for '运维告警群'` 即成功。

`OutgoingMessage` 带 `rich` 时会自动渲染为 `msgtype=markdown`（粗体 / 表格 / 链接卡片原生显示），失败回退纯文本。

### 3. 通用 Webhook（测试 / 自集成） ⚠️ 未实测

> 接口已挂载（`backend/gateways/webhook.py` + 路由），作者本人未在生产链路里跑过。同样**请自行调试**。

应用启动后自动挂载 `POST /api/gateways/webhook`。任何能发 HTTP 的客户端都可以模拟 IM 消息：

```powershell
$body = @{
    platform = "webhook"
    user_id  = "alice"
    text     = "今天天气怎么样"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri http://localhost:8020/api/gateways/webhook `
  -ContentType "application/json" -Body $body
```

返回 `{"status":"accepted","message_id":"..."}`，agent 处理完之后把回复打到 `zlagent` 容器 stderr（webhook 是 push-only 没有真实 outbound 通道）。可用于跑 e2e 测试 / 自己接其它 IM。

### 4. 排查清单

| 现象 | 排查点 |
|---|---|
| `weixin reload` 报 `gateway not registered` | `pip install aiohttp cryptography qrcode`（缺一不可） |
| 扫码后 CLI 卡死 480s | 网络无法访问 `ilinkai.weixin.qq.com`，需放行 |
| 测试消息超时，日志里 `ret=-2` | iLink 临时限流；等 1-3 分钟后 `POST /api/delivery-targets/{id}/test` 重试 |
| 发消息 errcode=-14 | session 过期，gateway 会自动重连一次；连续多次失败需重新跑 `weixin_login` |
| DeepSeek / OpenAI key 明明改了但仍报旧 key 401 | `docker compose restart` 不刷新 env；用 `up -d --force-recreate zlagent` |
| WeCom 群机器人 errcode!=0 | 看 `workspace/logs` 或 `docker compose logs zlagent`；常见原因：webhook key 拼错 / 群机器人被禁用 / 频率超限 |
| `/api/delivery-targets/{id}/test` 502 | gateway 没注册成功，先看 `/api/gateways` 列表里是否含目标 platform |

健康概览：`GET /api/gateways` 列出所有已注册 gateway 的 kind / configured / 收发计数。

---

## API 概览

| 功能 | 方法 | 路径 |
|---|---|---|
| 健康检查 | GET | `/api/health` |
| 八组件自检 | GET | `/api/doctor` |
| 运行时概览 | GET | `/api/runtime` |
| 网关列表与计数 | GET | `/api/gateways` |
| 记忆 CRUD | GET/POST/DELETE | `/api/memory` |
| 记忆 pin / archive | POST | `/api/memory/{id}/pin` |
| 技能列表 | GET | `/api/skills` |
| 技能审查触发 | POST | `/api/curator/run` |
| 定时任务 CRUD | GET/POST/DELETE | `/api/cron` |
| 投递目标 | GET/POST | `/api/delivery-targets` |
| 等待确认 | GET | `/api/confirmations` |
| 工具列表 | GET | `/api/tools` |
| 工具直测 | POST | `/api/tools/{name}/test` |
| LLM 状态 | GET | `/api/llm/status` |
| LLM 连通性测试 | POST | `/api/llm/test` |
| MCP server 列表 | GET | `/api/mcp/servers` |
| MCP 工具列表 | GET | `/api/mcp/tools` |
| MCP server 注册 | POST | `/api/mcp/servers` |
| MCP server 删除 | DELETE | `/api/mcp/servers/{name}` |
| MCP server 重连 | POST | `/api/mcp/servers/{name}/reconnect` |
| MCP 安装 | POST | `/api/mcp/install` |
| MCP 安装 + 注册 | POST | `/api/mcp/install_and_add` |
| 已安装包列表 | GET | `/api/mcp/installed?package_manager=npm` |
| 知识库列表 | GET | `/api/knowledge-bases` |
| 知识库图谱 | GET | `/api/knowledge-bases/{id}/graph` |
| 图谱重抽取 | POST | `/api/knowledge-bases/{id}/rebuild-graph` |
| 论文批量导入 | POST | `/api/knowledge-bases/{id}/papers/import` |
| GraphRAG 总览 | GET | `/api/graph-rag` |
| Wiki 缓存查询 | GET | `/api/wiki` |
| Review 状态 / 触发 | GET/POST | `/api/review/state` / `/api/review/run` |
| 维护任务 | POST | `/api/maintenance/run` |
| 夜间图谱任务 | POST | `/api/nightly/graph/run` |
| 插件列表 | GET | `/api/plugins` |

---

## 版本状态说明

- **已验证**：Docker 三服务启动、微信接入、LLM 连通性、离线 demo、GraphRAG smoke、MCP stdio/http 端到端脚本
- **部分验证**：企业微信群机器人、通用 Webhook、部分知识模式

知识写入相关边界：

- **wiki 不是启动后天然有内容**：`backend/wiki/`、crystallizer 和 `/api/wiki` 存在，但需要技能配置、知识写入或复盘链路触发；干净环境里 `/api/wiki` 返回空列表是正常状态。
- **GraphRAG smoke 验证的是快照链路**：当前 smoke 主要验证 LLM extractor、cache、builder 和快照生成；GraphRAG dashboard 读取的是运行时构建的 snapshot。

---

## 阅读路径

如果想理解架构，不建议从目录树开始读。更好的顺序是：

| 想理解什么 | 先看哪里 | 读完应该明白 |
|---|---|---|
| 系统怎么启动 | `backend/app.py` + `backend/bootstrap/` | FastAPI lifespan 如何组装运行时、路由、网关和后台服务 |
| 每轮对话怎么跑 | `backend/agent/loop.py` + `backend/agent/turn_preparer/` | 用户消息如何变成一次 turn，模型如何决定回复或调用工具 |
| 上下文怎么管理 | `backend/agent/context/` | 长对话如何压缩、记忆如何注入、turn scope 如何隔离 |
| 工具为什么安全 | `backend/tools/` + `backend/agent/confirmation/` | 能力如何注册，什么时候直接执行，什么时候需要用户确认 |
| MCP 怎么接入 | `backend/mcp/` | 外部 server 如何安装、连接、暴露成工具、进入权限体系 |
| 知识怎么写入 | `backend/memory/` + `backend/wiki/` + `backend/tools/builtins/knowledge_ingest.py` + `backend/graph/` | 偏好、答案缓存、文件知识库和图谱快照如何分层 |
| 微信怎么接入 | `backend/gateways/weixin.py` + `backend/cli/weixin_login.py` | 扫码凭据、热加载、收消息、发回复的链路 |

这比完整目录树更接近项目真实结构：ZLAgent 是围绕一次 agent turn 组织起来的，不是按文件夹平铺出来的。

---

## 验证与质量

```bash
docker compose exec -T -u root zlagent sh -lc 'rm -rf /app/scripts && mkdir -p /app/scripts'
docker cp scripts/. zlagent:/app/scripts/
docker compose exec -T -u root zlagent sh -lc 'chown -R zlagent:zlagent /app/scripts && chmod -R u=rwX,go=rX /app/scripts'

docker compose exec -T zlagent python -m compileall -q backend scripts
docker compose exec -T zlagent python scripts/smoke_llm_graph.py
docker compose exec -T zlagent python scripts/mcp_e2e_check.py
docker compose exec -T zlagent python scripts/mcp_e2e_http_check.py
docker compose exec -T zlagent env ZLAGENT_DEMO_FAST=1 python scripts/demo_e2e.py
```

---

## 配置项

完整字段见 `backend/core/config.py`。所有项支持环境变量覆盖（前缀 `ZLAGENT_` 或工具自带前缀如 `OPENAI_`）。

```env
# LLM
OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_MODEL=

# 可选 intent router（Flash 小模型预筛）
ZLAGENT_ROUTER_LLM_ENABLED=false
ZLAGENT_ROUTER_LLM_MODEL=

# 记忆
ZLAGENT_MEMORY_MAX_ENTRIES=200
ZLAGENT_MEMORY_MAX_ENTRY_CHARS=500

# 工具安全
ZLAGENT_TOOL_GUARDRAILS_HARD_STOP_ENABLED=true
ZLAGENT_SKILL_GUARD_ENABLED=true
ZLAGENT_SKILL_GUARD_STRICT_FOR_AGENT=true

# 上下文压缩
ZLAGENT_CONTEXT_SUMMARY_ENABLED=true
ZLAGENT_CONTEXT_SUMMARY_THRESHOLD_CHARS=49152

# GraphRAG
ZLAGENT_GRAPH_LLM_ENABLED=true

# 后端服务
DATABASE_URL=postgresql+psycopg://zlagent:zlagent@postgres:5432/zlagent
ZLAGENT_REDIS_URL=redis://redis:6379/0

# MCP
ZLAGENT_MCP_ENABLED=true

# 网页搜索（全部可选；不配也能跑 DuckDuckGo + Bing）
TAVILY_API_KEY=
ZLAGENT_SEARXNG_URL=
```

---

## License

本项目代码以 **MIT License** 发布。

项目设计参考了以下开源项目，公开发布时请遵守各自的许可与署名要求：

- **[learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)**（MIT，shareAI-lab）
  - **架构参考**：参考它逐步搭建 Agent 的思路，包括主流程、工具、权限、技能加载、记忆、失败处理、后台任务、定时任务和 MCP 扩展；ZLAgent 把这些思路改成 IM 常驻助理场景，而不是照搬代码
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)**（MIT，© 2025 Nous Research）
  - **代码 vendored**：`backend/gateways/_vendor/weixin_ilink.py` 是 Hermes `gateway/platforms/weixin.py` 的 iLink Bot 协议精简移植（MIT 协议文头已内嵌）
  - **设计参考、Python 重新实现**：参考长期记忆、工具安全、历史压缩、技能管理、定时任务和部分技能设计；相关实现主要在 `backend/memory/`、`backend/agent/`、`backend/tools/`、`workspace/skills/`
- **[OpenClaw](https://github.com/steipete/openclaw)**（MIT，© 2025 Peter Steinberger）
  - **设计参考、Python 重新实现**：参考工具安全分级、危险操作确认和 MCP 安装安全策略，位于 `backend/tools/` 与 `backend/mcp/`
- **[nvk/llm-wiki](https://github.com/nvk/llm-wiki)**（公开设计笔记，无源码 vendor）
  - **设计参考、Python 重新实现**：参考“能复用就不要重复生成”的 wiki 缓存思路，并实现事实抽取、来源记录、置信度和概念页整理，主要在 `backend/wiki/` 与 `backend/skills/`
- **[andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills)**（MIT，Forrest Chang 整理自 [Andrej Karpathy 推文](https://x.com/karpathy/status/2015883857489522876)）
  - **提示词参考**：四条原则（Think Before Coding / Simplicity First / Surgical Changes / Goal-Driven Execution）译写为中文常量，用在系统提示词和技能复盘提示词里

公开发布前建议补齐独立的 `THIRD_PARTY_NOTICES.md`，把每个上游 MIT 全文和 vendored 文件来源集中登记；当前 README 先保留来源和改编范围说明。
