"""Visited-map builder.

Renders a force-directed graph of cities the agent has been asked
about, derived from the v0.39.2 ``geo_path`` tags written by the
crystallizer. Visual rules (per user spec):

* **Nodes (cities)** — default blue ``#1f6feb``; turn red ``#d73a49``
  when at least one OD edge involving the city has been searched
  ``>= red_threshold`` times (default 10). City is plotted only if
  it appears in any ``crystal_kind=atomic_fact`` row's ``geo_path``.
* **Edge type 1 — same-province cluster** — implicit via echarts
  ``categories`` colouring (no real edge drawn; the force layout
  pulls same-province cities together because they share a colour
  group / category index). Keeps the graph from being a hairball.
* **Edge type 2 — OD pair** — drawn when two cities co-occur in the
  same fact's ``geo_path``. Yellow ``#f5a623`` when 1-9 hits, red
  ``#d73a49`` when ``>= red_threshold``. Width grows with ``log(hits)``.
* **Tooltip** — only "permanent knowledge" claims are surfaced;
  realtime skills (``travel-realtime-mcp``) are filtered out so
  weather / 12306 ticket ephemera doesn't pollute the map.

The builder is intentionally pure: ``build()`` returns a JSON-ready
dict, ``render_html()`` wraps it in a self-contained echarts page.
This split lets the REST endpoint stream the dict and the CLI write
the HTML — same data, two surfaces.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from loguru import logger


# Default blacklist: realtime skills generate facts whose validity is
# bounded (weather expires within hours, 12306 within minutes), so
# surfacing them as "permanent knowledge about this city" misleads
# the user. Add more skill ids here as new realtime tools land.
_DEFAULT_BLACKLIST: frozenset[str] = frozenset({
    "travel-realtime-mcp",
})

# Hits at or above this threshold turn an OD edge red AND pull both
# endpoints to red. Per user requirement: "if same topic searched
# > 10 times, mark the two points red".
_RED_THRESHOLD: int = 10

# Hard cap on rows pulled from the wiki for a single map render.
# Most installations will stay well under this; the cap defends
# against runaway queries on a wiki that's grown to hundreds of
# thousands of facts.
_MAX_ENTRIES: int = 2000

# Hit-cost scoring: each fact contributes ``hit_count + 1`` toward
# its city. The +1 is a basic-asked-once weight so a fact that's
# only been written (never re-hit) still shows up.
def _row_score(row: dict) -> int:
    return int(row.get("hit_count") or 0) + 1


def _parse_geo_path(geo_path: str) -> list[tuple[str, str]]:
    """Parse ``"city:北京;city:上海"`` → ``[("city", "北京"), ("city", "上海")]``.

    Defensive: skips empty / malformed tokens. Never raises.
    """
    if not geo_path:
        return []
    out: list[tuple[str, str]] = []
    for raw in geo_path.split(";"):
        raw = raw.strip()
        if not raw or ":" not in raw:
            continue
        type_, _, name = raw.partition(":")
        type_ = type_.strip()
        name = name.strip()
        if type_ and name:
            out.append((type_, name))
    return out


@dataclass
class _NodeAccumulator:
    """Per-city aggregate; one instance per distinct ``short_name``."""
    short_name: str
    score: int = 0
    fact_count: int = 0
    # (claim, hit_count, created_at_iso) — sorted by hit_count desc
    # before rendering, capped at the top N for the tooltip.
    facts: list[tuple[str, int, str]] = field(default_factory=list)


@dataclass
class _EdgeAccumulator:
    """Per OD-pair aggregate. Keys are normalised to ``(min, max)``
    so an undirected edge is uniquely keyed regardless of order.
    """
    a: str
    b: str
    weight: int = 0
    fact_count: int = 0


class VisitedMapBuilder:
    """Convert wiki + geo data into an echarts ``graph`` series.

    Construct once with the dependency stores, call :meth:`build` for
    JSON or :meth:`render_html` for a standalone page. The instance
    caches nothing — every call re-queries the underlying stores so
    the map is always live.
    """

    def __init__(
        self,
        wiki_store,  # backend.wiki.store.WikiStore
        geo_store,   # backend.wiki.geo_store.GeoStore
        *,
        blacklist_skills: Optional[Iterable[str]] = None,
        max_entries: int = _MAX_ENTRIES,
        red_threshold: int = _RED_THRESHOLD,
    ) -> None:
        self._wiki = wiki_store
        self._geo = geo_store
        self._blacklist: frozenset[str] = (
            frozenset(blacklist_skills)
            if blacklist_skills is not None
            else _DEFAULT_BLACKLIST
        )
        self._max_entries = max(1, int(max_entries))
        # Lower bound 2 so a misconfigured threshold of 0/1 doesn't
        # turn the entire map red on first hit.
        self._red = max(2, int(red_threshold))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, *, since_days: Optional[int] = None) -> dict:
        """Return an echarts-ready data dict.

        Schema::

            {
              "nodes": [...],      # echarts graph nodes
              "edges": [...],      # echarts graph edges
              "categories": [...], # one entry per distinct province
              "meta": {            # for the page header / debugging
                "total_cities": int,
                "total_edges": int,
                "total_facts": int,
                "generated_at": ISO8601,
                "since_days": int | null,
                "red_threshold": int,
                "blacklist": [skill_id, ...],
              },
            }

        ``since_days`` filters by ``created_at`` cutoff; ``None``
        (default) means "all time".
        """
        rows = self._fetch_rows()
        cutoff = self._build_cutoff(since_days)

        nodes, edges = self._aggregate(rows, cutoff=cutoff)

        # Resolve geographic metadata. Drops cities the GeoStore
        # doesn't recognise (their geo_path was probably written by
        # a stale seed or an external import).
        node_lat_lng, node_to_province = self._resolve_geo(nodes)

        # Drop accumulators for unresolved cities AFTER resolution
        # so the caller never sees orphan rows.
        nodes = {k: v for k, v in nodes.items() if k in node_to_province}
        edges = {
            k: e
            for k, e in edges.items()
            if k[0] in nodes and k[1] in nodes
        }

        # Compute "max OD weight per city" — drives the red-node
        # rule. A city goes red iff any incident edge crossed the
        # threshold; isolated cities never go red.
        node_max_edge: dict[str, int] = {}
        for (a, b), e in edges.items():
            if e.weight > node_max_edge.get(a, 0):
                node_max_edge[a] = e.weight
            if e.weight > node_max_edge.get(b, 0):
                node_max_edge[b] = e.weight

        # Province → category index.
        province_names = sorted(set(node_to_province.values()))
        category_index = {name: i for i, name in enumerate(province_names)}

        echarts_nodes = self._emit_nodes(
            nodes,
            node_to_province=node_to_province,
            node_lat_lng=node_lat_lng,
            node_max_edge=node_max_edge,
            category_index=category_index,
        )
        echarts_edges = self._emit_edges(edges)
        echarts_categories = [{"name": p} for p in province_names]

        return {
            "nodes": echarts_nodes,
            "edges": echarts_edges,
            "categories": echarts_categories,
            "meta": {
                "total_cities": len(echarts_nodes),
                "total_edges": len(echarts_edges),
                "total_facts": sum(n.fact_count for n in nodes.values()),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "since_days": since_days,
                "red_threshold": self._red,
                "blacklist": sorted(self._blacklist),
            },
        }

    def render_html(self, data: Optional[dict] = None) -> str:
        """Wrap :meth:`build` (or a pre-built dict) in a standalone
        echarts page.

        The page is fully self-contained: opens with a CDN script
        tag (jsdelivr primary, cdnjs fallback). No build step, no
        Python at view time. Drop the file on a flash drive and it
        still works.

        Empty maps render an explanatory placeholder rather than
        a broken-looking blank chart.
        """
        if data is None:
            data = self.build()
        if not data["nodes"]:
            return _EMPTY_HTML
        json_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return _HTML_TEMPLATE.replace("__GRAPH_DATA__", json_str)

    def write_html(self, output_path: Path, *, since_days: Optional[int] = None) -> Path:
        """Build and persist HTML to ``output_path``. Returns the
        resolved path. Parent directory is auto-created.
        """
        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        html = self.render_html(self.build(since_days=since_days))
        output_path.write_text(html, encoding="utf-8")
        return output_path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch_rows(self) -> list[dict]:
        """Pull wiki rows. Lives in its own method so tests can monkeypatch."""
        return list(self._wiki.list_entries(limit=self._max_entries))

    @staticmethod
    def _build_cutoff(since_days: Optional[int]) -> Optional[datetime]:
        if since_days is None or since_days <= 0:
            return None
        return datetime.now(timezone.utc) - timedelta(days=int(since_days))

    def _aggregate(
        self,
        rows: list[dict],
        *,
        cutoff: Optional[datetime],
    ) -> tuple[dict[str, _NodeAccumulator], dict[tuple[str, str], _EdgeAccumulator]]:
        """Roll up rows into per-city + per-OD accumulators.

        Filters applied (in order):

        1. ``crystal_kind == "atomic_fact"`` — only crystallised
           facts; full ``answer`` rows are too long for tooltips.
        2. ``skill_id NOT IN blacklist`` — drop realtime/ephemeral.
        3. ``created_at >= cutoff`` when ``since_days`` is set.
        4. ``geo_path`` parses to at least one ``city:`` token.
        """
        nodes: dict[str, _NodeAccumulator] = {}
        edges: dict[tuple[str, str], _EdgeAccumulator] = {}
        for row in rows:
            if row.get("crystal_kind") != "atomic_fact":
                continue
            if row.get("skill_id") in self._blacklist:
                continue
            if cutoff is not None:
                created_dt = self._parse_iso(row.get("created_at"))
                if created_dt is None or created_dt < cutoff:
                    continue
            tokens = [
                name for type_, name in _parse_geo_path(row.get("geo_path") or "")
                if type_ == "city"
            ]
            if not tokens:
                continue

            score = _row_score(row)
            claim = (row.get("answer") or "").strip()
            ts = row.get("created_at") or ""

            # Per-city accumulation.
            for c in tokens:
                acc = nodes.get(c)
                if acc is None:
                    acc = _NodeAccumulator(c)
                    nodes[c] = acc
                acc.score += score
                acc.fact_count += 1
                if claim:
                    acc.facts.append((claim, int(row.get("hit_count") or 0), ts))

            # Per-OD accumulation. Use sorted tuple so undirected
            # edges share a key. A self-loop (same city listed twice
            # in geo_path) is silently dropped.
            unique_cities = list(dict.fromkeys(tokens))  # preserve order, dedup
            for i in range(len(unique_cities)):
                for j in range(i + 1, len(unique_cities)):
                    a, b = sorted([unique_cities[i], unique_cities[j]])
                    if a == b:
                        continue
                    edge = edges.get((a, b))
                    if edge is None:
                        edge = _EdgeAccumulator(a, b)
                        edges[(a, b)] = edge
                    edge.weight += score
                    edge.fact_count += 1
        return nodes, edges

    @staticmethod
    def _parse_iso(value: object) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _resolve_geo(
        self,
        nodes: dict[str, _NodeAccumulator],
    ) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
        """Resolve every city to (lat, lng) + province via GeoStore.

        Cities the store doesn't know about are dropped with a log
        warning; the caller filters their accumulators out after.
        """
        lat_lng: dict[str, tuple[float, float]] = {}
        to_province: dict[str, str] = {}
        for short_name in nodes:
            geo_nodes = self._geo.find_by_name(short_name, type_="city")
            if not geo_nodes:
                logger.warning(
                    "[visited-map] dropping unknown city {!r} (not in GeoStore)",
                    short_name,
                )
                continue
            geo_node = geo_nodes[0]
            if geo_node.latitude is not None and geo_node.longitude is not None:
                lat_lng[short_name] = (geo_node.latitude, geo_node.longitude)
            ancestors = self._geo.ancestors_of(geo_node)
            # First ancestor is the immediate parent (province for a
            # city). Direct-administered cities (北京/上海/天津/重庆)
            # have a parent province with the same short_name, which
            # naturally puts them in their own group.
            province_name = ancestors[0].short_name if ancestors else short_name
            to_province[short_name] = province_name
        return lat_lng, to_province

    def _emit_nodes(
        self,
        nodes: dict[str, _NodeAccumulator],
        *,
        node_to_province: dict[str, str],
        node_lat_lng: dict[str, tuple[float, float]],
        node_max_edge: dict[str, int],
        category_index: dict[str, int],
    ) -> list[dict]:
        out: list[dict] = []
        for short_name, acc in nodes.items():
            province = node_to_province.get(short_name, short_name)
            is_red = node_max_edge.get(short_name, 0) >= self._red
            color = "#d73a49" if is_red else "#1f6feb"
            # Symbol size scales with score, log-clamped to keep
            # mega-cities from dwarfing everyone else.
            symbol_size = min(50.0, 18.0 + math.log1p(acc.score) * 6.0)
            top_facts = sorted(acc.facts, key=lambda x: -x[1])[:5]
            facts_html = "<br/>".join(
                "• " + (c[:80] + "…" if len(c) > 80 else c)
                for c, _, _ in top_facts
            ) or "<i>(暂无具体事实)</i>"
            tooltip_html = (
                f"<b>{short_name}</b> "
                f"<span style='color:#888'>({province})</span><br/>"
                f"score: {acc.score} · 涉及 {acc.fact_count} 条事实"
                f"<hr style='margin:6px 0;border:none;border-top:1px solid #eee'/>"
                f"<div style='max-width:320px;font-size:12px;line-height:1.5'>"
                f"<b>已知永久知识 (top {len(top_facts)})</b>:<br/>{facts_html}"
                f"</div>"
            )
            node_dict: dict = {
                "name": short_name,
                "category": category_index.get(province, 0),
                "symbolSize": round(symbol_size, 1),
                "value": acc.score,
                "itemStyle": {"color": color},
                "tooltip": {"formatter": tooltip_html},
            }
            # Geographic positioning: lng → x, -lat → y (echarts y
            # points down, so we flip lat). Force layout will still
            # nudge from these starting positions for legibility.
            lat_lng = node_lat_lng.get(short_name)
            if lat_lng is not None:
                node_dict["x"] = lat_lng[1]
                node_dict["y"] = -lat_lng[0]
            out.append(node_dict)
        return out

    def _emit_edges(
        self,
        edges: dict[tuple[str, str], _EdgeAccumulator],
    ) -> list[dict]:
        out: list[dict] = []
        for (_a, _b), e in edges.items():
            is_red = e.weight >= self._red
            color = "#d73a49" if is_red else "#f5a623"
            # Width: 1.5 px floor so single-hit edges stay visible.
            width = max(1.5, math.log1p(e.weight) * 1.5)
            out.append({
                "source": e.a,
                "target": e.b,
                "value": e.weight,
                "lineStyle": {
                    "color": color,
                    "width": round(width, 2),
                    "opacity": 0.85,
                    "curveness": 0.1,
                },
                "tooltip": {
                    "formatter": (
                        f"{e.a} ↔ {e.b}<br/>"
                        f"搜过 {e.weight} 次 · 涉及 {e.fact_count} 条事实"
                    ),
                },
            })
        return out


# ----------------------------------------------------------------------
# HTML templates — kept as module constants so they're easy to diff /
# audit without grepping a string concat. ``__GRAPH_DATA__`` is the
# single substitution token; everything else is static.
# ----------------------------------------------------------------------

_EMPTY_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>ZLAgent · 我走过的地方</title>
<style>
body{margin:0;padding:48px;font-family:-apple-system,BlinkMacSystemFont,
"PingFang SC","Microsoft YaHei",sans-serif;background:#fafafa;color:#444;
text-align:center}
h1{font-size:22px;color:#333}
p{font-size:14px;line-height:1.7;max-width:560px;margin:8px auto}
code{background:#eee;padding:2px 6px;border-radius:3px;font-size:13px}
</style></head><body>
<h1>🗺️ 我走过的地方</h1>
<p>还没有带城市标签的事实写进 wiki。</p>
<p>试试在 IM 里问一个带地名的问题，例如<br/>
<code>帮我规划北京三日游</code> 或 <code>京沪高铁多久</code>，<br/>
等 crystallizer 抽完事实再来看这张图。</p>
</body></html>
"""


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>ZLAgent · 我走过的地方</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"
        onerror="this.onerror=null;this.src='https://cdnjs.cloudflare.com/ajax/libs/echarts/5.4.3/echarts.min.js'"></script>
<style>
  *{box-sizing:border-box}
  body{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,
       "PingFang SC","Microsoft YaHei",sans-serif;background:#fafafa;color:#333}
  #header{padding:14px 24px;border-bottom:1px solid #eee;background:#fff;
          display:flex;align-items:center;justify-content:space-between}
  #header .title{font-size:18px;font-weight:600}
  #header .meta{color:#888;font-size:12px}
  #chart{width:100%;height:calc(100vh - 60px)}
  #legend{position:fixed;right:20px;top:80px;background:#fff;
          border:1px solid #eaeaea;border-radius:8px;padding:14px 16px;
          font-size:12px;box-shadow:0 2px 12px rgba(0,0,0,0.06);
          line-height:1.7;z-index:10}
  #legend .row{display:flex;align-items:center}
  #legend .dot{width:11px;height:11px;border-radius:50%;margin-right:8px}
  #legend .line{width:22px;height:2.5px;margin-right:8px;border-radius:1px}
  #legend b{display:block;margin-bottom:4px;color:#555}
</style>
</head>
<body>
<div id="header">
  <div class="title">🗺️ 我走过的地方</div>
  <div class="meta" id="meta"></div>
</div>
<div id="chart"></div>
<div id="legend">
  <b>节点</b>
  <div class="row"><div class="dot" style="background:#1f6feb"></div>普通城市</div>
  <div class="row"><div class="dot" style="background:#d73a49"></div>≥10 次</div>
  <b style="margin-top:8px">连接</b>
  <div class="row"><div class="line" style="background:#f5a623"></div>搜过</div>
  <div class="row"><div class="line" style="background:#d73a49"></div>≥10 次</div>
</div>
<script>
const DATA = __GRAPH_DATA__;

document.getElementById('meta').textContent = (
  '城市 ' + DATA.meta.total_cities +
  ' · 连接 ' + DATA.meta.total_edges +
  ' · 事实 ' + DATA.meta.total_facts +
  ' · ' + (DATA.meta.generated_at || '').slice(0, 10)
);

const chart = echarts.init(document.getElementById('chart'));
chart.setOption({
  tooltip: { trigger: 'item', confine: true, enterable: true,
             extraCssText: 'max-width:360px;white-space:normal' },
  legend: [{
    data: DATA.categories.map(c => c.name),
    top: 8, type: 'scroll', textStyle: { fontSize: 11 }
  }],
  series: [{
    type: 'graph',
    layout: 'force',
    data: DATA.nodes,
    edges: DATA.edges,
    categories: DATA.categories,
    roam: true,
    draggable: true,
    label: {
      show: true, position: 'right',
      formatter: '{b}', fontSize: 12, color: '#333'
    },
    force: {
      repulsion: 240,
      gravity: 0.05,
      edgeLength: [60, 180],
      friction: 0.4,
      layoutAnimation: true
    },
    emphasis: {
      focus: 'adjacency',
      label: { fontSize: 14, fontWeight: 'bold' },
      lineStyle: { width: 5 }
    },
    lineStyle: { opacity: 0.85 },
    edgeSymbol: ['none', 'none']
  }]
});

window.addEventListener('resize', () => chart.resize());
</script>
</body>
</html>
"""
