# 运行指南

如何在 Windows / Linux 上把 ZLAgent 跑起来，以及如何同时观察各路日志。

---

## OpenZLAgent 一键启动

当前仓库已经把 ZLAgent 和 OpenGUI backend 合并到根目录启动脚本里。

```bash
cd /Users/kirito/Desktop/OpenZLAgent
./start.sh
```

这个命令会：

- 后台启动 OpenGUI backend：`http://localhost:7777`
- 启动 ZLAgent Docker 服务：`http://localhost:8020`
- 自动让 ZLAgent 容器通过 `http://host.docker.internal:7777` 调用 OpenGUI

第一次需要安装/启动手机端时，USB 连接 Android 手机并开启 USB 调试，然后运行：

```bash
./start.sh --with-phone
```

常用命令：

```bash
./status.sh
./stop.sh
docker compose logs -f zlagent
tail -f workspace/logs/opengui-server.log
docker compose run --rm weixin-login
```

手机端仍需手动允许 Android 权限：USB 调试、无障碍服务、悬浮窗权限，必要时关闭电池优化。

---

## 当前本地启动顺序

只启动 ZLAgent 主服务：

```bash
cd <OpenZLAgent>
docker compose up -d --build
```

窗口 1 查看应用主日志：

```bash
docker compose logs -f --tail=100 zlagent
```

链接微信：

```bash
docker compose run --rm weixin-login
```

连接 Android 无线调试手机时，把占位符替换为手机“无线调试”页面显示的地址和端口：

```bash
cd <OpenZLAgent>
./phone-wifi.sh pair <pair_ip:pair_port>
./phone-wifi.sh connect <device_ip:adb_port>
```

注意：`phone-wifi.sh` 只负责 ADB 配对、`adb reverse tcp:7777`、拉起手机端和查看设备。OpenGUI backend 需要已经通过 `./start.sh` 或其他方式运行在 `http://localhost:7777`。

---

## 0. 前置依赖

| 依赖 | 版本 | 必需 | 说明 |
|---|---|---|---|
| Python | 3.11+ | ✅ | 本地直跑必需 |
| Docker Desktop | 最新 | 推荐 | 一键拉起 zlagent + postgres + redis |
| Git | 任意 | ✅ | 克隆代码 |
| Node.js / npm | 22+ | 推荐 | OpenGUI backend 和 npm 类 MCP server 需要 |
| `uv` / `uvx` | 可选 | 可选 | 装 Python 类 MCP server 用 |

LLM API key（DeepSeek / Qwen / OpenAI 任一兼容服务）— 不配也能起，但 agent 只能 echo。

---

## 1. 首次建立

### 1.1 克隆代码

```powershell
git clone <your-repo-url>
cd ZLAgent
```

### 1.2 配置环境变量

```powershell
copy .env.example .env
notepad .env
```

最少改这三项即可正常用：

```env
OPENAI_API_KEY=<your-llm-api-key>
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

完整字段见 `.env.example` 注释。

---

## 2. 启动方式（二选一）

### 方式 A：Docker 一键（推荐）

```powershell
docker compose up -d --build
```

会拉起三个容器：

| 服务 | 端口 | 用途 |
|---|---|---|
| `zlagent` | 8020 | 主应用 FastAPI |
| `zlagent-postgres` | 5432 | 业务数据 + pgvector |
| `zlagent-redis` | 6379 | 会话 / 缓存 LRU |

打开 `http://localhost:8020/api/health`，应返回 `{"status":"ok","version":"1.2.2"}`。

### 方式 B：本地 Python（开发用）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m backend.app
```

不带 Postgres 时会自动落回 `data/zlagent.db` SQLite；Redis 缺失时相应缓存能力降级，**不影响主对话流程**。当前 GraphRAG 是从 memory、knowledge mode 文件和 graph cache 构建快照，不再依赖 Neo4j。

---

## 3. 八组件自检

```powershell
curl http://localhost:8020/api/doctor
```

返回 LLM / DB / 工作区 / 技能 / Cron / 网关 / 记忆 / MCP 八项状态。任何一项 `error` 都会被列出。

---

## 4. 同时查看日志（重点）

ZLAgent 日志分为三路：

| 日志 | 内容 | 位置 |
|---|---|---|
| **应用主日志** | agent loop / IM 网关 / 路由 / 请求 / 异常 | Docker：容器 stdout；本地：`python` 进程 stderr |
| **MCP 安装日志** | npm / pip / uvx 装包过程的 stdout + stderr | `workspace/logs/mcp-install.log` |
| **MCP 运行日志** | 已连接 MCP server 子进程的 stderr | `workspace/logs/mcp-stderr.log` |

### 4.1 Docker 部署下并行 tail

**单终端跟一个**（够用）：

```powershell
docker compose logs -f zlagent
```

**三窗口同时观察**（推荐排查时使用）。开三个 PowerShell 窗口各跑一条：

```powershell
# 窗口 1：应用主日志（Docker stdout）
docker compose logs -f --tail=100 zlagent

# 窗口 2：MCP 安装管线（容器内文件）
docker compose exec zlagent powershell -Command "Get-Content -Path workspace/logs/mcp-install.log -Wait -Tail 50"

# 窗口 3：MCP 子进程 stderr
docker compose exec zlagent powershell -Command "Get-Content -Path workspace/logs/mcp-stderr.log -Wait -Tail 50"
```

如果容器是 Linux 镜像（默认）则把 `powershell -Command` 换成 `sh -c`：

```powershell
docker compose exec zlagent sh -c "tail -F workspace/logs/mcp-install.log"
docker compose exec zlagent sh -c "tail -F workspace/logs/mcp-stderr.log"
```

**单窗口三路合一**（PowerShell 后台 job）：

```powershell
$j1 = Start-Job { docker compose logs -f --no-color zlagent }
$j2 = Start-Job { docker compose exec -T zlagent sh -c "tail -F workspace/logs/mcp-install.log" }
$j3 = Start-Job { docker compose exec -T zlagent sh -c "tail -F workspace/logs/mcp-stderr.log" }

# 实时拉取三个 job 的输出
while ($true) {
    Receive-Job $j1, $j2, $j3
    Start-Sleep -Milliseconds 500
}

# 退出时清理
Stop-Job $j1, $j2, $j3 ; Remove-Job $j1, $j2, $j3
```

### 4.2 本地 Python 部署下并行 tail

应用本身已经把日志打到 stderr，所以启动那一窗就是主日志。再开两窗：

```powershell
# 窗口 1：启动主进程（同时即主日志）
python -m backend.app

# 窗口 2：MCP 安装日志
Get-Content -Path workspace\logs\mcp-install.log -Wait -Tail 50

# 窗口 3：MCP 运行日志
Get-Content -Path workspace\logs\mcp-stderr.log -Wait -Tail 50
```

PowerShell 的 `Get-Content -Wait` 等价于 Linux 的 `tail -F`，文件被截断 / 重命名也能正确跟随。

### 4.3 把主进程日志也落盘（可选）

主进程默认只打 stderr，如需保留历史，启动时重定向：

```powershell
python -m backend.app 2>&1 | Tee-Object -FilePath workspace\logs\app.log
```

之后第三个窗口就可以：

```powershell
Get-Content -Path workspace\logs\app.log -Wait -Tail 100
```

### 4.4 按级别过滤

启动前在 `.env` 改：

```env
ZLAGENT_LOG_LEVEL=DEBUG     # 默认 INFO；问题排查时调到 DEBUG
```

DEBUG 会包含每个 turn 的 prompt 长度、router LLM 决策、guardrail 签名等细节。生产环境建议保持 INFO。

### 4.5 实时跟某条会话

会话上下文落在：

```text
workspace/memory/session_context.jsonl
```

可同步 tail：

```powershell
Get-Content workspace\memory\session_context.jsonl -Wait -Tail 20
```

每行一个 JSON turn，含 `platform / user_id / role / content / timestamp`。

---

## 5. 常见问题排查

| 现象 | 检查 |
|---|---|
| `/api/health` 502 | `docker compose ps`，`zlagent` 容器是否 healthy |
| `/api/doctor` 显示 LLM down | `.env` 里 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` 三项是否都填 |
| GraphRAG 节点为空 | 是否有 memory / knowledge mode / runtime records；是否设置 `ZLAGENT_GRAPH_LLM_ENABLED=true` |
| MCP 工具装不上 | `workspace/logs/mcp-install.log` 看 npm / pip 真实报错；网络 / 包名 / `--ignore-scripts` |
| MCP 工具装上但调用 fail | `workspace/logs/mcp-stderr.log` 看子进程异常 |
| 微信收不到回复 | `docker compose logs zlagent | findstr weixin`；检查 `WEIXIN_BASE_URL` / Webhook 签名 |
| Postgres 启动慢 | 首次 init schema 约 10–20s，等 healthcheck 转绿 |

---

## 6. 停止 / 重启 / 清理

### 6.1 软停（保留数据）

```powershell
docker compose stop
```

### 6.2 重启某个服务

```powershell
docker compose restart zlagent
```

### 6.3 完全停（保留 named volume）

```powershell
docker compose down
```

### 6.4 完全清空（连数据卷一起删）

```powershell
docker compose down -v
Remove-Item -Recurse -Force workspace\logs\*, data\zlagent.db -ErrorAction SilentlyContinue
```

⚠️ **不可逆**：会清空所有长期记忆、cron 任务、MCP server 配置、知识库和图谱缓存。

---

## 7. 验证脚本

```powershell
python -m compileall -q backend scripts          # 编译检查
python scripts\smoke_llm_graph.py                 # GraphRAG LLM 抽取闭环
python scripts\mcp_e2e_check.py                   # MCP stdio 端到端
python scripts\mcp_e2e_http_check.py              # MCP HTTP 端到端
python scripts\demo_e2e.py                        # 离线 demo（无需 LLM key）
```

每个脚本都自带 assert，exit 0 即通过。
