"""Health and introspection endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import __version__
from ..core.config import get_settings
from ..core.runtime import build_core_runtime_overview
from ..graph.source import collect_graph_records
from ..graph.builder import KnowledgeGraphBuilder

root_router = APIRouter(tags=["root"])
router = APIRouter(prefix="/api", tags=["health"])


@root_router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root_landing() -> str:
    settings = get_settings()
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\"/>
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
<title>{settings.app_name} v{__version__}</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #0b1020;
  --panel: rgba(15, 23, 42, 0.88);
  --panel-2: rgba(30, 41, 59, 0.82);
  --border: rgba(148, 163, 184, 0.18);
  --text: #e2e8f0;
  --muted: #94a3b8;
  --accent: #60a5fa;
  --accent-2: #34d399;
  --shadow: 0 24px 80px rgba(2, 6, 23, 0.45);
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  min-height: 100vh;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  background:
    radial-gradient(circle at top left, rgba(96, 165, 250, 0.18), transparent 35%),
    radial-gradient(circle at top right, rgba(52, 211, 153, 0.14), transparent 30%),
    linear-gradient(180deg, #060913 0%, var(--bg) 100%);
  color: var(--text);
}}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.shell {{ max-width: 1280px; margin: 0 auto; padding: 32px 20px 48px; }}
.hero {{
  background: linear-gradient(135deg, rgba(30, 41, 59, 0.88), rgba(15, 23, 42, 0.72));
  border: 1px solid var(--border);
  border-radius: 24px;
  padding: 28px;
  box-shadow: var(--shadow);
}}
.hero h1 {{ margin: 0; font-size: clamp(28px, 3vw, 44px); line-height: 1.1; }}
.hero p {{ margin: 12px 0 0; color: var(--muted); max-width: 72ch; }}
.badges {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
.badge {{
  padding: 8px 12px; border: 1px solid var(--border); border-radius: 999px;
  background: rgba(15, 23, 42, 0.55); color: var(--text); font-size: 13px;
}}
.grid {{ display: grid; gap: 18px; margin-top: 18px; grid-template-columns: repeat(12, 1fr); }}
.card {{
  grid-column: span 4; background: var(--panel); border: 1px solid var(--border);
  border-radius: 20px; padding: 20px; box-shadow: var(--shadow); min-height: 220px;
}}
.card.wide {{ grid-column: span 8; }}
.card.full {{ grid-column: span 12; }}
.card h2 {{ margin: 0 0 10px; font-size: 18px; }}
.card .muted {{ color: var(--muted); font-size: 14px; }}
.list {{ display: grid; gap: 10px; margin-top: 14px; }}
.item {{ background: var(--panel-2); border: 1px solid var(--border); border-radius: 14px; padding: 12px 14px; }}
.item strong {{ display: block; margin-bottom: 4px; }}
.kv {{ display: grid; grid-template-columns: 1fr auto; gap: 8px 12px; align-items: center; }}
.kv div {{ padding: 8px 0; border-bottom: 1px solid rgba(148, 163, 184, 0.12); }}
.kv div:nth-last-child(-n+2) {{ border-bottom: 0; }}
.pill {{ display:inline-block; padding: 4px 10px; border-radius: 999px; background: rgba(96, 165, 250, 0.12); color: #bfdbfe; margin-left: 8px; font-size: 12px; }}
.footer {{ margin-top: 18px; color: var(--muted); font-size: 13px; }}
@media (max-width: 980px) {{ .card, .card.wide {{ grid-column: span 12; }} }}
code {{ background: rgba(148, 163, 184, 0.14); padding: 2px 6px; border-radius: 6px; }}
</style>
</head>
<body>
<div class=\"shell\">
  <section class=\"hero\">
    <h1>{settings.app_name}</h1>
    <p>一个面向 IM 的常驻 Agent 平台。页面把运行时拆成 <strong>核心区</strong> 与 <strong>外部扩展区</strong>，方便后续接入论文工具、效率工具、MCP 和插件市场。</p>
    <div class=\"badges\">
      <span class=\"badge\">v{__version__}</span>
      <span class=\"badge\">core + tools runtime</span>
      <span class=\"badge\">core / extensions / services</span>
      <span class=\"badge\">ready for plugin market</span>
    </div>
  </section>

  <section class=\"grid\">
    <article class=\"card wide\">
      <h2>核心区 <span class=\"pill\">Core</span></h2>
      <div class=\"muted\">系统内核、LLM、工具总线、可观测性与进度反馈。</div>
      <div id=\"core-block\" class=\"list\"></div>
    </article>

    <article class=\"card\">
      <h2>服务状态 <span class=\"pill\">Services</span></h2>
      <div id=\"service-block\" class=\"list\"></div>
    </article>

    <article class=\"card full\">
      <h2>外部扩展区 <span class=\"pill\">Extensions</span></h2>
      <div class=\"muted\">用户安装的技能、MCP、插件与外部工具，会在这里分组展示。</div>
      <div id=\"extension-block\" class=\"grid\" style=\"margin-top:14px;\"></div>
    </article>

    <article class=\"card full\">
      <h2>接口入口</h2>
      <div class=\"kv\">
        <div><a href=\"/api/health\">/api/health</a></div><div>健康检查</div>
        <div><a href=\"/api/info\">/api/info</a></div><div>运行时总览</div>
        <div><a href=\"/api/runtime\">/api/runtime</a></div><div>运行时 JSON</div>
        <div><a href=\"/api/harness/extensions\">/api/harness/extensions</a></div><div>用户扩展清单</div>
        <div><a href=\"/api/harness/metrics\">/api/harness/metrics</a></div><div>运行指标</div>
        <div><a href=\"/docs\">/docs</a></div><div>Swagger UI</div>
      </div>
      <div class=\"footer\">Webhook 入口：<code>POST /api/gateways/webhook</code> · 定时链路：<code>delivery-target → cron → gateway → IM</code></div>
    </article>
  </section>
</div>
<script>
async function loadOverview() {{
  const res = await fetch('/api/info');
  const data = await res.json();
  const core = data.core || {{}};
  const extensions = data.extensions || {{}};
  const services = data.services || {{}};
  const extGroups = extensions.extensions || extensions;

  const coreBlock = document.getElementById('core-block');
  coreBlock.innerHTML = `
    <div class=\"item\"><strong>工具总数</strong><span>${{core.tools?.count ?? 0}}</span></div>
    <div class=\"item\"><strong>LLM 配置</strong><span>${{core.llm_configured ? '已配置' : '未配置'}}</span></div>
    <div class=\"item\"><strong>进度反馈</strong><span>${{core.progress_enabled ? '已启用' : '未启用'}}</span></div>
    <div class=\"item\"><strong>Tracing</strong><span>${{core.tracing_enabled ? '已启用' : '未启用'}}</span></div>
    <div class=\"item\"><strong>Memo</strong><span>${{core.memo_enabled ? '已启用' : '未启用'}}</span></div>
  `;

  const serviceBlock = document.getElementById('service-block');
  serviceBlock.innerHTML = Object.entries(services).map(([name, value]) => `
    <div class=\"item\"><strong>${{name}}</strong><span>${{value ? 'on' : 'off'}}</span></div>
  `).join('');

  const extensionBlock = document.getElementById('extension-block');
  const cards = [
    ['Skills', extGroups.skills_items || extGroups.skills || []],
    ['MCPs', extGroups.mcps_items || extGroups.mcps || []],
    ['Plugins', extGroups.plugins_items || extGroups.plugins || []],
    ['Tools', extGroups.tools_items || extGroups.tools || []],
  ];
  extensionBlock.innerHTML = cards.map(([title, items]) => `
    <article class=\"card\" style=\"grid-column: span 6; min-height: 180px;\">
      <h2>${{title}} <span class=\"pill\">${{items.length}}</span></h2>
      <div class=\"muted\">${{items.length ? '已发现可用扩展' : '暂无可展示条目'}}</div>
      <div class=\"list\">${{items.slice(0, 4).map((row) => `
        <div class=\"item\"><strong>${{row.name || row.id || row.title || 'untitled'}}</strong><span>${{row.description || row.status || row.permission || ''}}</span></div>
      `).join('')}}</div>
    </article>
  `).join('');
}}
loadOverview().catch((err) => {{
  console.error(err);
  document.body.insertAdjacentHTML('afterbegin', '<div style="padding:12px;color:#fca5a5">加载运行时概览失败</div>');
}});
</script>
</body>
</html>"""


@router.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": __version__,
    }


@router.get("/info")
async def info(request: Request) -> dict[str, Any]:
    settings = get_settings()
    overview = build_core_runtime_overview(app=request.app).to_dict()
    return {
        "app": settings.app_name,
        "version": __version__,
        "log_level": settings.log_level,
        "paths": {
            "data_dir": str(settings.data_dir),
            "config_dir": str(settings.config_dir),
            "workspace_dir": str(settings.workspace_dir),
        },
        "channels": {
            "wecom": settings.wecom_enabled,
            "feishu": settings.feishu_enabled,
            "email": settings.email_enabled,
            "telegram": settings.telegram_enabled,
        },
        "llm": {
            "provider": settings.llm_provider,
            "configured": bool(
                (settings.openai_api_key or settings.openai_base_url != "https://api.openai.com/v1")
                and (settings.openai_model or settings.default_model)
            ),
            "model": settings.openai_model or settings.default_model,
            "base_url": settings.openai_base_url,
        },
        "tools": overview["core"]["tools"],
        "core": overview["core"],
        "extensions": overview["extensions"],
        "services": overview["services"],
    }


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    settings = get_settings()
    overview = build_core_runtime_overview(app=request.app).to_dict()
    records = collect_graph_records()
    graph = KnowledgeGraphBuilder().build_from_records(records.records)
    knowledge_bases = [
        {
            "id": "default",
            "name": "默认记忆",
            "kind": "memory",
            "summary": "长期偏好、环境事实与项目上下文。",
            "status": "active",
            "count": len(graph.nodes),
            "template": "runtime graph",
            "nodes": [
                {
                    "id": node.id,
                    "label": node.label,
                    "type": node.kind,
                    "x": 0,
                    "y": 0,
                    "detail": str(node.attrs.get("summary") or ""),
                }
                for node in graph.nodes[:12]
            ],
            "edges": [
                [edge.source, edge.target]
                for edge in graph.edges[:18]
                if edge.source and edge.target
            ],
        }
    ]
    return {
        "currentKbId": "default",
        "showGlobal": True,
        "selectedNode": knowledge_bases[0]["nodes"][0] if knowledge_bases[0]["nodes"] else None,
        "knowledgeBases": knowledge_bases,
        "tasks": [
            {"title": "同步图谱", "detail": "从后端实时导出知识图谱。", "status": "待执行"},
            {"title": "检索加速", "detail": "结合图结构与向量召回做候选收缩。", "status": "进行中"},
        ],
        "logs": [
            f"{settings.app_name} dashboard loaded",
            f"core tools={overview['core']['tools']['count']} extensions={len(overview['extensions'].get('tools_items', []))}",
        ],
    }
