from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..base import Tool, ToolPermission, ToolResult

_MODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# added ``study`` template after the user reported that
# building an "agent 八股" (AI Agent interview-prep) knowledge base
# with the bare ``generic`` template gave entity_types=[entity] which
# the LLM then improvised into a misnamed "应试八股" schema. The
# ``study`` template carries the right defaults for any "organise my
# learning notes / interview prep / concept network" use case so the
# LLM does not have to hallucinate a schema.
_VALID_TEMPLATES = frozenset({"travel", "ai-paper", "study", "generic"})
_REQUIRED_DIRS = (
    "raw",
    "raw/assets",
    "wiki",
    "wiki/sources",
    "wiki/concepts",
    "wiki/entities",
    "wiki/outputs",
    "wiki/insights",
)
_REQUIRED_FILES = ("MODE.md", "schema.md", "wiki/index.md", "wiki/log.md", "wiki/graph.json")


class _KnowledgeModeBase(Tool):
    """Shared filesystem helpers for the knowledge-mode tool pair.

    v0.40.1 split the original single tool into two so read-only
    inspection (``list`` / ``templates`` / ``read`` / ``lint``) can run
    without the per-call confirmation prompt that wrecks the chat
    rhythm. The mutating ``create`` action stays behind a CONFIRM tool.
    The two subclasses share this base for path resolution and the
    template text so the directory rules stay in one place.
    """

    def __init__(self, workspace_dir: Path) -> None:
        self._workspace = Path(workspace_dir).resolve()
        self._root = (self._workspace / "knowledge_modes").resolve()

    def _mode_dir_from_args(self, arguments: dict[str, Any]) -> Path | ToolResult:
        mode_id = str(arguments.get("mode_id") or "").strip()
        if not mode_id:
            return ToolResult(ok=False, content="", error="mode_id is required")
        if not _MODE_ID_RE.match(mode_id):
            return ToolResult(
                ok=False,
                content="",
                error="mode_id must match [a-z0-9][a-z0-9._-]{0,63}",
            )
        mode_dir = (self._root / mode_id).resolve()
        try:
            mode_dir.relative_to(self._root)
        except ValueError:
            return ToolResult(ok=False, content="", error="mode path escapes knowledge_modes root")
        return mode_dir

    @staticmethod
    def _templates_text() -> str:
        return (
            "Available templates:\n"
            "- travel: province/city/place itinerary wiki with geo-style entities.\n"
            "- ai-paper: daily paper digest wiki with paper/author/method/topic entities.\n"
            "- study: interview-prep / 技术八股 / concept-map / course notes —\n"
            "  topic/question/framework/pitfall/tradeoff. Pick this one for any\n"
            "  '组织学习资料 / 面试题 / agent 八股 / cheat sheet' use case.\n"
            "- generic: minimal LLM Wiki skeleton for a custom domain — usually\n"
            "  reach for one of the named templates above instead."
        )

    def _list(self) -> ToolResult:
        if not self._root.exists():
            return ToolResult(ok=True, content="(no knowledge modes yet)", raw={"items": []})
        items: list[dict[str, str]] = []
        for path in sorted(self._root.iterdir()):
            if not path.is_dir() or not (path / "MODE.md").exists():
                continue
            meta = _read_mode_meta(path / "MODE.md")
            items.append({
                "mode_id": path.name,
                "template": meta.get("template", "unknown"),
                "title": meta.get("title", path.name),
                "description": meta.get("description", ""),
            })
        if not items:
            return ToolResult(ok=True, content="(no knowledge modes yet)", raw={"items": []})
        lines = [
            f"- {item['mode_id']} [{item['template']}] {item['title']} — {item['description']}"
            for item in items
        ]
        return ToolResult(ok=True, content="\n".join(lines), raw={"items": items})

    def _read(self, arguments: dict[str, Any]) -> ToolResult:
        mode_dir = self._mode_dir_from_args(arguments)
        if isinstance(mode_dir, ToolResult):
            return mode_dir
        file_key = str(arguments.get("file") or "mode").strip().lower()
        rel_by_key = {
            "mode": "MODE.md",
            "schema": "schema.md",
            "index": "wiki/index.md",
            "log": "wiki/log.md",
            "graph": "wiki/graph.json",
        }
        rel = rel_by_key.get(file_key)
        if rel is None:
            return ToolResult(ok=False, content="", error="file must be one of mode/schema/index/log/graph")
        target = mode_dir / rel
        if not target.exists():
            return ToolResult(ok=False, content="", error=f"{rel} not found for mode {mode_dir.name!r}")
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"read failed: {exc}")
        return ToolResult(ok=True, content=content[: self.max_result_chars])

    def _lint(self, arguments: dict[str, Any]) -> ToolResult:
        mode_dir = self._mode_dir_from_args(arguments)
        if isinstance(mode_dir, ToolResult):
            return mode_dir
        missing_dirs = [rel for rel in _REQUIRED_DIRS if not (mode_dir / rel).is_dir()]
        missing_files = [rel for rel in _REQUIRED_FILES if not (mode_dir / rel).is_file()]
        ok = not missing_dirs and not missing_files
        data = {"mode_id": mode_dir.name, "ok": ok, "missing_dirs": missing_dirs, "missing_files": missing_files}
        if ok:
            return ToolResult(ok=True, content=f"Knowledge mode '{mode_dir.name}' lint OK.", raw=data)
        return ToolResult(
            ok=False,
            content="",
            error=(
                f"Knowledge mode '{mode_dir.name}' is incomplete;"
                f" missing_dirs={missing_dirs}; missing_files={missing_files}"
            ),
            raw=data,
        )


class KnowledgeInspectTool(_KnowledgeModeBase):
    """Read-only inspection of the knowledge-mode catalog.

    Split out from ``KnowledgeModeManageTool`` in v0.40.1 so the agent
    can answer questions like "how many KBs do I have?" or "show me
    the travel schema" without a yes/no round trip per call. The tool
    is SAFE because every action is read-only: it lists directories,
    returns the static template description, reads a single text file,
    or lint-walks a mode's required paths. None of these mutate disk
    state, so confirmation here is pure friction.
    """

    name = "knowledge_inspect"
    description = (
        "Read-only inspector for domain knowledge-mode wikis under"
        " `workspace/knowledge_modes/`. Use this whenever the user asks"
        " what knowledge bases exist, which templates are available, or"
        " what a particular mode's schema / log / graph looks like. This"
        " tool never mutates state, so it runs without confirmation —"
        " prefer it over `knowledge_mode_manage` for any non-create ask.\n\n"
        "Actions:\n"
        "- list: enumerate every mode under knowledge_modes/.\n"
        "- templates: list the bundled templates (travel / ai-paper / generic).\n"
        "- read: dump one file (mode | schema | index | log | graph) of a mode.\n"
        "- lint: report missing directories or files for a mode."
    )
    permission = ToolPermission.SAFE
    is_read_only = True
    is_concurrency_safe = True
    is_destructive = False
    max_result_chars = 8_000
    search_hint = "knowledge mode wiki list templates read schema graph lint inspect"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["templates", "list", "read", "lint"],
                "description": "Read-only operation to perform.",
            },
            "mode_id": {
                "type": "string",
                "description": "Required for read/lint. Lowercase slug of the mode.",
            },
            "file": {
                "type": "string",
                "enum": ["mode", "schema", "index", "log", "graph"],
                "description": "Which file `read` should fetch. Defaults to mode.",
            },
        },
        "required": ["action"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        if not action:
            return ToolResult(ok=False, content="", error="action is required")
        if action == "templates":
            return ToolResult(ok=True, content=self._templates_text())
        if action == "list":
            return self._list()
        if action == "read":
            return self._read(arguments)
        if action == "lint":
            return self._lint(arguments)
        return ToolResult(ok=False, content="", error=f"unknown action: {action}")


class KnowledgeModeManageTool(_KnowledgeModeBase):
    """Mutating operations on the knowledge-mode catalog.

    Trimmed to a single action (``create``) in v0.40.1. Read paths
    moved to :class:`KnowledgeInspectTool`. Permission stays CONFIRM
    because creating a mode writes a five-file template skeleton under
    workspace/knowledge_modes/<mode_id>/ and we want the user to see
    the chosen template + mode_id before that lands on disk.
    """

    name = "knowledge_mode_manage"
    description = (
        "Create a new domain knowledge-mode wiki under"
        " `workspace/knowledge_modes/`. A knowledge mode owns a domain"
        " schema, raw sources, generated wiki pages, entity/concept"
        " directories, an operation log, and a graph placeholder."
        " Permission tier is confirm because create mutates durable"
        " workspace state.\n\n"
        "Workflow: first call `knowledge_inspect` action=list to see"
        " existing modes; if none match the user's requested domain,"
        " ask whether to create one; after approval call action=create.\n\n"
        "Template selection guide (v0.40.5):\n"
        "- `travel` — 旅行 / 出行 / 地理： province/city/route/itinerary.\n"
        "- `ai-paper` — 论文检索 / 科研跟踪： paper/author/method/dataset.\n"
        "- `study` — 面试备考 / 技术八股 / 概念图谱 / 课程笔记：\n"
        "  topic/question/framework/pitfall. Use this when the user says\n"
        "  '八股' / 'interview' / '技术笔记' / '概念图谱' / 'cheat sheet'\n"
        "  / '面试题'. Crucially: do NOT pick `generic` for these — `study`\n"
        "  is purpose-built so the LLM does not have to hallucinate the\n"
        "  schema like it did for the v0.40.4 'agent 八股' regression.\n"
        "- `generic` — 都不匹配时的占位。一般应该反问用户到底是\n"
        "  哪个场景，而不是默认选这个。\n\n"
        "This is the ONLY action this tool exposes — list, templates,"
        " read and lint live on `knowledge_inspect` (also SAFE)."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = False
    max_result_chars = 8_000
    search_hint = "knowledge mode create domain wiki llm wiki schema template travel paper"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create"],
                "description": "Must be 'create' — read paths live on knowledge_inspect.",
            },
            "mode_id": {
                "type": "string",
                "description": "Lowercase slug for the new mode, e.g. travel or ai-paper.",
            },
            "template": {
                "type": "string",
                "enum": sorted(_VALID_TEMPLATES),
                "description": "Template to instantiate. Defaults to generic.",
            },
            "title": {
                "type": "string",
                "description": "Human-readable title for the mode.",
            },
            "description": {
                "type": "string",
                "description": "Short purpose statement.",
            },
        },
        "required": ["action", "mode_id"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        if not action:
            return ToolResult(ok=False, content="", error="action is required")
        if action == "create":
            return self._create(arguments)
        return ToolResult(
            ok=False,
            content="",
            error=(
                f"unknown action: {action!r}; this tool only handles 'create'. "
                "Use knowledge_inspect for list/templates/read/lint."
            ),
        )

    def _create(self, arguments: dict[str, Any]) -> ToolResult:
        mode_dir = self._mode_dir_from_args(arguments)
        if isinstance(mode_dir, ToolResult):
            return mode_dir
        mode_id = mode_dir.name
        template = str(arguments.get("template") or "generic").strip().lower()
        if template not in _VALID_TEMPLATES:
            return ToolResult(
                ok=False,
                content="",
                error=f"template must be one of {sorted(_VALID_TEMPLATES)}",
            )
        if mode_dir.exists():
            return ToolResult(
                ok=False,
                content="",
                error=f"knowledge mode {mode_id!r} already exists; use read/list/lint",
            )

        spec = _template_spec(template, mode_id, arguments)
        try:
            for rel in _REQUIRED_DIRS:
                (mode_dir / rel).mkdir(parents=True, exist_ok=True)
            for rel in spec["entity_dirs"]:
                (mode_dir / "wiki" / "entities" / rel).mkdir(parents=True, exist_ok=True)
            for rel in spec["concept_dirs"]:
                (mode_dir / "wiki" / "concepts" / rel).mkdir(parents=True, exist_ok=True)
            (mode_dir / "MODE.md").write_text(spec["mode_md"], encoding="utf-8")
            (mode_dir / "schema.md").write_text(spec["schema_md"], encoding="utf-8")
            (mode_dir / "wiki" / "index.md").write_text(spec["index_md"], encoding="utf-8")
            (mode_dir / "wiki" / "log.md").write_text(spec["log_md"], encoding="utf-8")
            (mode_dir / "wiki" / "graph.json").write_text(
                json.dumps(spec["graph"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return ToolResult(ok=False, content="", error=f"create failed: {exc}")

        return ToolResult(
            ok=True,
            content=(
                f"Knowledge mode '{mode_id}' created with template '{template}'.\n"
                f"  root: knowledge_modes/{mode_id}\n"
                "  files: MODE.md, schema.md, wiki/index.md, wiki/log.md, wiki/graph.json\n"
                f"  entities: {', '.join(spec['entity_dirs']) or '(none)'}\n"
                f"  concepts: {', '.join(spec['concept_dirs']) or '(none)'}"
            ),
            raw={"mode_id": mode_id, "template": template, "root": f"knowledge_modes/{mode_id}"},
        )


def _read_mode_meta(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("-") or ":" not in line:
            continue
        key, value = line[1:].split(":", 1)
        key = key.strip()
        if key in {"title", "template", "description"}:
            out[key] = value.strip()
    return out


def _template_spec(template: str, mode_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    title = str(arguments.get("title") or _default_title(template, mode_id)).strip()
    description = str(arguments.get("description") or _default_description(template)).strip()
    entity_dirs, concept_dirs, relationships, durable, transient = _domain_parts(template)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mode_md = _mode_md(mode_id, template, title, description, entity_dirs, concept_dirs)
    schema_md = _schema_md(title, description, entity_dirs, concept_dirs, relationships, durable, transient)
    index_md = f"# {title} Index\n\n- mode: [[../MODE.md|{mode_id}]]\n- schema: [[../schema.md|schema]]\n\n## Pages\n\n暂无页面。\n"
    log_md = f"# {title} Log\n\n## [{now}] create | {mode_id}\n\nCreated knowledge mode from `{template}` template.\n"
    graph = {
        "schema_version": "knowledge-mode-v1",
        "mode_id": mode_id,
        "template": template,
        "created_at": now,
        "nodes": [],
        "edges": [],
    }
    return {
        "entity_dirs": entity_dirs,
        "concept_dirs": concept_dirs,
        "mode_md": mode_md,
        "schema_md": schema_md,
        "index_md": index_md,
        "log_md": log_md,
        "graph": graph,
    }


def _default_title(template: str, mode_id: str) -> str:
    if template == "travel":
        return "旅行规划知识库"
    if template == "ai-paper":
        return "AI 论文知识库"
    if template == "study":
        return "学习知识库"
    return mode_id.replace("-", " ").title()


def _default_description(template: str) -> str:
    if template == "travel":
        return "按省、市、地点、路线和出行约束沉淀非时效旅行知识。"
    if template == "ai-paper":
        return "按论文、作者、机构、方法、数据集和主题沉淀每日 AI 论文知识。"
    if template == "study":
        return (
            "按主题 → 子主题 → 题目/概念 三层组织学习资料："
            "适合面试备考、技术八股、课程笔记、概念图谱之类场景。结构"
            "上以“题目 + 框架 + 陷阱 + 交叉引用”为主，区别于 travel"
            "（重实体地理）和 ai-paper（重论文实体）。"
        )
    return "面向自定义领域的 LLM Wiki 知识库。"


def _domain_parts(template: str) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    if template == "travel":
        return (
            ["province", "city", "district", "attraction", "restaurant", "hotel_area", "transport_hub", "route", "itinerary", "season", "budget_level"],
            ["itinerary_style", "transport", "traveler_type", "pitfall"],
            ["belongs_to", "located_in", "near", "connects", "includes", "suitable_for", "avoid_when", "supports", "contradicts", "supersedes"],
            ["地理层级", "景点与区域", "经典路线", "适合人群", "长期避坑经验", "非实时交通常识"],
            ["当前天气", "实时余票", "临时门票价格", "突发限流", "当天营业状态"],
        )
    if template == "ai-paper":
        return (
            ["paper", "author", "institution", "method", "dataset", "benchmark", "code_repo", "venue"],
            ["topic", "architecture", "evaluation", "research_trend"],
            ["authored_by", "affiliated_with", "belongs_to_topic", "uses_method", "evaluates_on", "compares_with", "has_code", "supports", "contradicts", "supersedes"],
            ["论文元数据", "方法贡献", "数据集与 benchmark", "作者/机构关系", "可复用主题综述"],
            ["今日热度", "临时排名", "未核验社媒评价", "下载失败的占位信息"],
        )
    if template == "study":
        # “study” 模板：面试备考 / 技术八股 / 课程笔记 / 概念图谱场景。
        # entity 层是具体可引用的潜在资料（题目、参考、实验），
        # concept 层是跨题复用的模枋（框架、技巧、陷阱）。关系名
        # 重在 “拓展 / 驳斥 / 依赖 / 动机” 这几个学习场景高频动词。
        return (
            [
                "topic", "subtopic", "question", "reference", "example",
                "experiment", "course_module", "author",
            ],
            [
                "framework", "technique", "pitfall", "tradeoff",
                "paradigm", "checklist",
            ],
            [
                "belongs_to", "extends", "contrasts_with", "depends_on",
                "motivates", "answers", "cites", "refutes",
                "supports", "contradicts", "supersedes",
            ],
            [
                "面试 / 考试高频题与参考答案",
                "可复用的讨论框架 / 试验设计 / 护栏策略",
                "常见陷阱与错误原因剖析",
                "概念间的依赖、拓展与替代关系",
                "参考论文 / 代码 / 文档的原始出处",
            ],
            [
                "某次具体面试的现场问答记录",
                "未核验的个人记忆",
                "过时的 API 文档片段 / 库版本号",
                "社媒 / 社区未验证的传闻",
            ],
        )
    return (
        ["entity"],
        ["concept"],
        ["mentions", "supports", "contradicts", "builds_on", "questions", "supersedes"],
        ["被来源支持的稳定事实", "实体关系", "可复用结论"],
        ["一次性聊天细节", "未验证猜测", "过期状态"],
    )


def _mode_md(mode_id: str, template: str, title: str, description: str, entity_dirs: list[str], concept_dirs: list[str]) -> str:
    return (
        f"# {title}\n\n"
        f"- mode_id: {mode_id}\n"
        f"- template: {template}\n"
        f"- title: {title}\n"
        f"- description: {description}\n\n"
        "## Purpose\n\n"
        f"{description}\n\n"
        "## Structure\n\n"
        "- `raw/`: immutable sources and assets.\n"
        "- `wiki/index.md`: catalog of pages.\n"
        "- `wiki/log.md`: append-only operation log.\n"
        "- `wiki/entities/`: typed entity pages.\n"
        "- `wiki/concepts/`: cross-source concept pages.\n"
        "- `wiki/outputs/`: valuable answers filed back into the wiki.\n"
        "- `wiki/graph.json`: typed graph export placeholder.\n\n"
        f"## Entity Types\n\n{_bullet_list(entity_dirs)}\n\n"
        f"## Concept Types\n\n{_bullet_list(concept_dirs)}\n"
    )


def _schema_md(
    title: str,
    description: str,
    entity_dirs: list[str],
    concept_dirs: list[str],
    relationships: list[str],
    durable: list[str],
    transient: list[str],
) -> str:
    return (
        f"# {title} Schema\n\n"
        "## Goal\n\n"
        f"{description}\n\n"
        "## Three-Layer Architecture\n\n"
        "1. `raw/` stores immutable source material.\n"
        "2. `wiki/` stores LLM-maintained pages, outputs, and graph artifacts.\n"
        "3. `schema.md` defines domain conventions and maintenance rules.\n\n"
        "## Operations\n\n"
        "### Ingest\n\n"
        "1. Read the source from `raw/` or a trusted tool result.\n"
        "2. Extract stable facts, entities, concepts, and relationships.\n"
        "3. Create or update pages under `wiki/entities/` and `wiki/concepts/`.\n"
        "4. Update `wiki/index.md` and append `wiki/log.md`.\n"
        "5. Preserve uncertainty, source names, and contradictions.\n\n"
        "### Query\n\n"
        "1. Read `wiki/index.md` first.\n"
        "2. Read only the relevant pages.\n"
        "3. Answer with citations to source or wiki pages.\n"
        "4. File valuable non-transient answers under `wiki/outputs/`.\n\n"
        "### Lint\n\n"
        "Check for contradictions, stale claims, orphan pages, missing cross-links, duplicate entities, and knowledge gaps.\n\n"
        f"## Entity Types\n\n{_bullet_list(entity_dirs)}\n\n"
        f"## Concept Types\n\n{_bullet_list(concept_dirs)}\n\n"
        f"## Relationship Types\n\n{_bullet_list(relationships)}\n\n"
        f"## Durable Knowledge\n\n{_bullet_list(durable)}\n\n"
        f"## Transient Knowledge\n\nDo not store these as durable facts unless converted into a stable pattern:\n\n{_bullet_list(transient)}\n\n"
        "## Grounding Rules\n\n"
        "- Prefer raw sources and real tool output over old wiki summaries.\n"
        "- Do not hide contradictions; mark superseded claims explicitly.\n"
        "- Keep one fact/page self-contained enough to be useful next month.\n"
        "- Never store secrets, credentials, or private one-off chat details.\n"
    )


def _bullet_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- (none)"
