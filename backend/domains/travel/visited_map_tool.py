"""visited_map tool.

Text summary of cities the wiki knows about + a URL to the full
echarts HTML map. Triggered when a user asks something like
"我走过哪些地方" / "show me the cities I've asked about".

Safe tier (pure read): runs :class:`VisitedMapBuilder.build` against
the same wiki+geo stores the service uses, returns a Markdown table
plus the ``/api/wiki/visited-map`` URL the user can open in a
browser.
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from ...tools.base import Tool, ToolPermission, ToolResult


class VisitedMapTool(Tool):
    name = "visited_map"
    description = (
        "Summarise cities the agent has atomic facts about, with search "
        "counts per city and top OD pairs. Use when the user asks where "
        "they've 'been' or 'asked about', e.g. '我走过哪些地方', "
        "'show me the places I've asked about'. Returns a Markdown "
        "summary plus a URL to a full interactive graph (echarts-based "
        "HTML page). No arguments are required; optional since_days "
        "narrows to recent history."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    is_destructive = False
    max_result_chars = 4_000
    search_hint = (
        "visited map travel history cities asked wiki atomic fact "
        "geo graph echarts 地图 走过 去过"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "since_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3650,
                "description": (
                    "Only count facts created within the last N days. "
                    "Omit for all-time history."
                ),
            },
            "red_threshold": {
                "type": "integer",
                "minimum": 2,
                "maximum": 10000,
                "description": (
                    "Hit count at which a city/edge is considered "
                    "'heavily searched'. Default 10."
                ),
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": (
                    "How many top cities + top OD pairs to list in the "
                    "Markdown summary. Default 10."
                ),
            },
        },
        "required": [],
    }

    def __init__(
        self,
        wiki_store,
        geo_store,
        *,
        base_url: str = "http://localhost:8020",
    ) -> None:
        self._wiki = wiki_store
        self._geo = geo_store
        # ``base_url`` is the externally-reachable origin of the API
        # surface. We only use it to assemble a clickable link for
        # the user; if the operator reverse-proxies ZLAgent behind
        # a public hostname, set this accordingly at construction.
        self._base_url = base_url.rstrip("/")

    def activity_description(self, arguments: dict[str, Any]) -> str:
        since = arguments.get("since_days")
        if since:
            return f"visited_map (last {int(since)}d)"
        return "visited_map"

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if self._wiki is None or self._geo is None:
            return ToolResult(
                ok=False,
                content="",
                error=(
                    "visited_map unavailable: wiki_store or geo_store "
                    "not initialised."
                ),
            )

        since_days = arguments.get("since_days")
        red_threshold = int(arguments.get("red_threshold") or 10)
        top_k = int(arguments.get("top_k") or 10)

        # Lazy import so this module never pulls echarts HTML into
        # memory unless the tool actually runs.
        from .visited_map import VisitedMapBuilder

        try:
            builder = VisitedMapBuilder(
                self._wiki,
                self._geo,
                red_threshold=red_threshold,
            )
            data = builder.build(
                since_days=int(since_days) if since_days else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[visited_map] build crashed: {}", exc)
            return ToolResult(
                ok=False, content="", error=f"build failed: {exc}"
            )

        return ToolResult(ok=True, content=self._format(data, top_k=top_k))

    # ------------------------------------------------------------------
    # Markdown rendering — kept text-based so every IM channel can
    # consume it (WeCom / Weixin both accept Markdown; plain-text
    # channels fall back cleanly since there are no raw HTML tags).
    # ------------------------------------------------------------------

    def _format(self, data: dict, *, top_k: int) -> str:
        meta = data["meta"]
        nodes = data["nodes"]
        edges = data["edges"]

        url = f"{self._base_url}/api/wiki/visited-map"
        if meta.get("since_days"):
            url += f"?since_days={meta['since_days']}"

        if not nodes:
            return (
                "🗺️ **我走过的地方** · 暂无数据\n\n"
                "还没有带城市标签的事实写进 wiki。在 IM 里问一个带地名的"
                "问题试试（比如 _帮我规划北京三日游_），等 crystallizer 抽完"
                "事实就能看到图了。\n\n"
                f"📊 完整交互图：{url}"
            )

        # Sort cities by score desc, then name.
        top_cities = sorted(
            nodes, key=lambda n: (-n.get("value", 0), n.get("name", ""))
        )[:top_k]
        top_edges = sorted(
            edges, key=lambda e: -e.get("value", 0)
        )[:top_k]

        lines: list[str] = []
        lines.append("🗺️ **我走过的地方**")
        lines.append(
            f"共 **{meta['total_cities']}** 个城市 · "
            f"**{meta['total_edges']}** 条连接 · "
            f"**{meta['total_facts']}** 条事实"
        )
        if meta.get("since_days"):
            lines.append(f"_（近 {meta['since_days']} 天）_")
        lines.append("")

        lines.append("### 🏙️ 搜索最多的城市")
        lines.append("| 排名 | 城市 | 分数 | 颜色 |")
        lines.append("|---|---|---|---|")
        for i, node in enumerate(top_cities, start=1):
            color = node.get("itemStyle", {}).get("color", "#1f6feb")
            marker = "🔴" if color == "#d73a49" else "🔵"
            lines.append(
                f"| {i} | **{node['name']}** | {node.get('value', 0)} | {marker} |"
            )

        if top_edges:
            lines.append("")
            lines.append("### 🔗 最多的城市对")
            lines.append("| 排名 | 连接 | 次数 | 颜色 |")
            lines.append("|---|---|---|---|")
            for i, edge in enumerate(top_edges, start=1):
                color = edge.get("lineStyle", {}).get("color", "#f5a623")
                marker = "🔴" if color == "#d73a49" else "🟡"
                lines.append(
                    f"| {i} | {edge['source']} ↔ {edge['target']} | "
                    f"{edge.get('value', 0)} | {marker} |"
                )

        lines.append("")
        lines.append(f"📊 完整交互图：{url}")
        return "\n".join(lines)
