"""Chinese project-effectiveness benchmark corpus and observer runner."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import Any


EFFECTIVENESS_SCHEMA_VERSION = 1
_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


class EffectivenessTrack(str, Enum):
    """Resume-relevant project evidence tracks."""

    INTENT_ROUTING = "intent_routing"
    HARNESS_RELIABILITY = "harness_reliability"
    MEMORY_CONTEXT = "memory_context"
    DAG_TOKEN = "dag_token"
    AGENT_TASK = "agent_task"


class EffectivenessExecutorKind(str, Enum):
    """Host-owned executor adapter selected by one case."""

    INTENT_ROUTER = "intent_router"
    RELEASE_FIXTURE = "release_fixture"
    MEMORY_FIXTURE = "memory_fixture"
    CONTEXT_FIXTURE = "context_fixture"
    DAG_FIXTURE = "dag_fixture"
    TOKEN_BUDGET_FIXTURE = "token_budget_fixture"
    CAPABILITY_FIXTURE = "capability_fixture"
    AGENT_TASK = "agent_task"


class EffectivenessSplit(str, Enum):
    """Lifecycle of a benchmark case."""

    PILOT = "pilot"
    DEVELOPMENT = "development"
    HOLDOUT = "holdout"


class CheckOperator(str, Enum):
    """Deterministic operators supported by the generic observer."""

    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"


@dataclass(frozen=True, slots=True)
class EffectivenessCheck:
    """One machine-checkable expectation over an executor fact path."""

    path: str
    operator: CheckOperator
    value: Any
    description_zh: str

    def __post_init__(self) -> None:
        if not _PATH_RE.fullmatch(self.path):
            raise ValueError(f"invalid effectiveness check path: {self.path}")
        if not self.description_zh.strip():
            raise ValueError("effectiveness check description_zh must be non-empty")
        if not _CJK_RE.search(self.description_zh):
            raise ValueError(
                "effectiveness check description_zh must contain Chinese text"
            )
        if self.operator in {
            CheckOperator.LT,
            CheckOperator.LTE,
            CheckOperator.GT,
            CheckOperator.GTE,
        } and not _is_number(self.value):
            raise ValueError(
                f"numeric effectiveness operator requires a number: {self.path}"
            )


@dataclass(frozen=True, slots=True)
class EffectivenessCase:
    """One independently editable Chinese JSONL benchmark scenario."""

    schema_version: int
    corpus_version: str
    id: str
    track: EffectivenessTrack
    executor: EffectivenessExecutorKind
    split: EffectivenessSplit
    name_zh: str
    scenario_zh: str
    repetitions: int
    input: dict[str, Any]
    checks: tuple[EffectivenessCheck, ...]
    capabilities: tuple[str, ...]
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.schema_version != EFFECTIVENESS_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported effectiveness case schema version: {self.schema_version}"
            )
        for value, label in (
            (self.corpus_version, "corpus_version"),
            (self.id, "id"),
            (self.name_zh, "name_zh"),
            (self.scenario_zh, "scenario_zh"),
        ):
            if not value.strip():
                raise ValueError(f"effectiveness case {label} must be non-empty")
        if not _CJK_RE.search(self.name_zh) or not _CJK_RE.search(self.scenario_zh):
            raise ValueError(
                "effectiveness case name_zh and scenario_zh must contain Chinese text"
            )
        if self.repetitions < 1:
            raise ValueError("effectiveness case repetitions must be >= 1")
        object.__setattr__(self, "input", dict(self.input))
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "tags", tuple(self.tags))
        if not self.checks:
            raise ValueError("effectiveness case must define checks")
        if not self.capabilities:
            raise ValueError("effectiveness case must declare capabilities")
        if any(not _PATH_RE.fullmatch(item) for item in self.capabilities):
            raise ValueError(
                "effectiveness case capabilities must use stable identifiers"
            )
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("effectiveness case capabilities must be unique")
        if any(not tag.strip() for tag in self.tags):
            raise ValueError("effectiveness case tags must be non-empty")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("effectiveness case tags must be unique")


@dataclass(frozen=True, slots=True)
class EffectivenessManifest:
    """Frozen corpus-level metadata stored separately from JSONL cases."""

    schema_version: int
    corpus_version: str
    language: str
    status: str
    description_zh: str
    required_tracks: tuple[EffectivenessTrack, ...]
    minimum_cases_by_track: dict[EffectivenessTrack, int]
    required_capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != EFFECTIVENESS_SCHEMA_VERSION:
            raise ValueError(
                "unsupported effectiveness manifest schema version: "
                f"{self.schema_version}"
            )
        if not self.corpus_version.strip():
            raise ValueError("effectiveness corpus_version must be non-empty")
        if self.language != "zh-CN":
            raise ValueError("effectiveness corpus language must be zh-CN")
        if self.status not in {"pilot", "frozen"}:
            raise ValueError("effectiveness corpus status must be pilot or frozen")
        if not self.description_zh.strip() or not _CJK_RE.search(self.description_zh):
            raise ValueError("effectiveness description_zh must contain Chinese text")
        object.__setattr__(self, "required_tracks", tuple(self.required_tracks))
        object.__setattr__(
            self,
            "minimum_cases_by_track",
            dict(self.minimum_cases_by_track),
        )
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(self.required_capabilities),
        )
        if not self.required_tracks:
            raise ValueError("effectiveness manifest must require tracks")
        if len(self.required_tracks) != len(set(self.required_tracks)):
            raise ValueError("effectiveness required_tracks must be unique")
        if set(self.minimum_cases_by_track) != set(self.required_tracks):
            raise ValueError(
                "effectiveness minimum_cases_by_track must match required_tracks"
            )
        if any(value < 1 for value in self.minimum_cases_by_track.values()):
            raise ValueError("effectiveness minimum case counts must be positive")
        if not self.required_capabilities:
            raise ValueError("effectiveness manifest must require capabilities")
        if any(not _PATH_RE.fullmatch(item) for item in self.required_capabilities):
            raise ValueError(
                "effectiveness required capabilities must use stable identifiers"
            )
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("effectiveness required capabilities must be unique")


@dataclass(frozen=True, slots=True)
class EffectivenessCorpus:
    """Strict manifest plus independently editable Chinese cases."""

    manifest: EffectivenessManifest
    cases: tuple[EffectivenessCase, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cases", tuple(self.cases))
        if not self.cases:
            raise ValueError("effectiveness corpus must contain cases")
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("effectiveness case ids must be unique")
        for case in self.cases:
            if case.corpus_version != self.manifest.corpus_version:
                raise ValueError(
                    f"case {case.id} corpus_version does not match manifest"
                )
        counts = {
            track: sum(case.track is track for case in self.cases)
            for track in self.manifest.required_tracks
        }
        missing = [
            f"{track.value}: {counts[track]} < {minimum}"
            for track, minimum in self.manifest.minimum_cases_by_track.items()
            if counts[track] < minimum
        ]
        if missing:
            raise ValueError(
                "effectiveness corpus has insufficient track coverage: "
                + ", ".join(missing)
            )
        covered = {
            capability for case in self.cases for capability in case.capabilities
        }
        missing_capabilities = sorted(
            set(self.manifest.required_capabilities).difference(covered)
        )
        if missing_capabilities:
            raise ValueError(
                "effectiveness corpus is missing capability coverage: "
                + ", ".join(missing_capabilities)
            )


@dataclass(frozen=True, slots=True)
class EffectivenessCaseResult:
    """One observed repetition compared with the frozen checks."""

    case_id: str
    case_name_zh: str
    track: EffectivenessTrack
    executor: EffectivenessExecutorKind
    capabilities: tuple[str, ...]
    repetition: int
    passed: bool
    duration_ms: float
    failures: tuple[str, ...] = field(default_factory=tuple)
    facts: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.repetition < 1:
            raise ValueError("effectiveness result repetition must be >= 1")
        if self.duration_ms < 0:
            raise ValueError("effectiveness result duration_ms must be >= 0")
        object.__setattr__(self, "failures", tuple(self.failures))
        object.__setattr__(self, "facts", dict(self.facts))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_name_zh": self.case_name_zh,
            "track": self.track.value,
            "executor": self.executor.value,
            "capabilities": list(self.capabilities),
            "repetition": self.repetition,
            "passed": self.passed,
            "duration_ms": round(self.duration_ms, 3),
            "failures": list(self.failures),
            "facts": dict(self.facts),
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class EffectivenessReport:
    """Machine-readable observations plus a compact Chinese summary."""

    corpus_version: str
    corpus_status: str
    results: tuple[EffectivenessCaseResult, ...]
    duration_ms: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        if not self.results:
            raise ValueError("effectiveness report must contain results")

    @property
    def passed_count(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed_count / len(self.results)

    @property
    def ok(self) -> bool:
        return self.passed_count == len(self.results)

    @property
    def task_completion_eligible(self) -> int:
        return sum(
            result.track is EffectivenessTrack.AGENT_TASK for result in self.results
        )

    @property
    def completed_tasks(self) -> int:
        return sum(
            result.track is EffectivenessTrack.AGENT_TASK
            and result.facts.get("accepted") is True
            and result.facts.get("status") == "completed"
            and result.facts.get("all_expected_evidence") is True
            and result.facts.get("acceptance_covers_expected") is True
            and result.facts.get("false_completion") == 0
            for result in self.results
        )

    @property
    def task_completion_rate(self) -> float | None:
        if self.task_completion_eligible == 0:
            return None
        return self.completed_tasks / self.task_completion_eligible

    @property
    def long_task_completion_eligible(self) -> int:
        return sum(
            result.track is EffectivenessTrack.AGENT_TASK
            and "long_task_continuation" in result.capabilities
            for result in self.results
        )

    @property
    def completed_long_tasks(self) -> int:
        return sum(
            result.track is EffectivenessTrack.AGENT_TASK
            and "long_task_continuation" in result.capabilities
            and result.facts.get("accepted") is True
            and result.facts.get("status") == "completed"
            and result.facts.get("all_expected_evidence") is True
            and result.facts.get("acceptance_covers_expected") is True
            and result.facts.get("false_completion") == 0
            for result in self.results
        )

    @property
    def long_task_completion_rate(self) -> float | None:
        if self.long_task_completion_eligible == 0:
            return None
        return self.completed_long_tasks / self.long_task_completion_eligible

    @property
    def false_completions(self) -> int:
        return sum(
            _numeric_fact(result.facts.get("false_completion"))
            for result in self.results
            if result.track is EffectivenessTrack.AGENT_TASK
        )

    @property
    def agent_task_token_usage(self) -> dict[str, int | float]:
        return _aggregate_usage(
            result
            for result in self.results
            if result.track is EffectivenessTrack.AGENT_TASK
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "corpus_version": self.corpus_version,
            "corpus_status": self.corpus_status,
            "duration_ms": round(self.duration_ms, 3),
            "aggregate": {
                "total": len(self.results),
                "passed": self.passed_count,
                "failed": len(self.results) - self.passed_count,
                "pass_rate": self.pass_rate,
                "task_completion_eligible": self.task_completion_eligible,
                "completed_tasks": self.completed_tasks,
                "task_completion_rate": self.task_completion_rate,
                "long_task_completion_eligible": (self.long_task_completion_eligible),
                "completed_long_tasks": self.completed_long_tasks,
                "long_task_completion_rate": self.long_task_completion_rate,
                "false_completions": self.false_completions,
                "agent_task_token_usage": self.agent_task_token_usage,
            },
            "by_track": self._by_track(),
            "by_capability": self._by_capability(),
            "results": [result.to_dict() for result in self.results],
        }

    def to_jsonl(self) -> str:
        """Serialize one result per line for later aggregation or diffing."""

        return (
            "\n".join(
                json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
                for result in self.results
            )
            + "\n"
        )

    def to_markdown(self) -> str:
        """Render a human-readable Chinese project evidence summary."""

        lines = [
            "# ZLAgent 效果 Benchmark",
            "",
            f"- 语料版本：`{self.corpus_version}`",
            f"- 语料状态：`{self.corpus_status}`",
            f"- 总结果数：{len(self.results)}",
            f"- 通过率：{self.pass_rate:.2%}",
            f"- 总耗时：{self.duration_ms:.3f} ms",
            "",
            "| 轨道 | 通过 | 总数 | 通过率 |",
            "| --- | ---: | ---: | ---: |",
        ]
        if self.task_completion_rate is not None:
            usage = self.agent_task_token_usage
            completion_lines = [
                f"- 真实任务完成率：{self.task_completion_rate:.2%} "
                f"({self.completed_tasks}/{self.task_completion_eligible})",
                f"- 真实任务误完成数：{self.false_completions}",
                f"- 真实任务 Token：总计 {usage['total_tokens']}，"
                f"平均 {usage['average_total_tokens']:.1f}，"
                f"P95 {usage['p95_total_tokens']}",
            ]
            if self.long_task_completion_rate is not None:
                completion_lines.insert(
                    1,
                    "- 真实长任务完成率："
                    f"{self.long_task_completion_rate:.2%} "
                    f"({self.completed_long_tasks}/"
                    f"{self.long_task_completion_eligible})",
                )
            lines[7:7] = completion_lines
        for track, values in self._by_track().items():
            lines.append(
                f"| `{track}` | {values['passed']} | {values['total']} | "
                f"{values['pass_rate']:.2%} |"
            )
        failures = [result for result in self.results if not result.passed]
        if failures:
            lines.extend(("", "## 失败项", ""))
            for result in failures:
                reason = (
                    "；".join(result.failures) or result.error_message or "未知错误"
                )
                lines.append(
                    f"- `{result.case_id}` 第 {result.repetition} 次：{reason}"
                )
        return "\n".join(lines) + "\n"

    def _by_track(self) -> dict[str, dict[str, int | float]]:
        grouped: dict[str, dict[str, int | float]] = {}
        for track in EffectivenessTrack:
            matching = [result for result in self.results if result.track is track]
            if not matching:
                continue
            passed = sum(result.passed for result in matching)
            grouped[track.value] = {
                "total": len(matching),
                "passed": passed,
                "failed": len(matching) - passed,
                "pass_rate": passed / len(matching),
            }
        return grouped

    def _by_capability(self) -> dict[str, dict[str, int | float]]:
        grouped: dict[str, dict[str, int | float]] = {}
        capabilities = sorted(
            {
                capability
                for result in self.results
                for capability in result.capabilities
            }
        )
        for capability in capabilities:
            matching = [
                result for result in self.results if capability in result.capabilities
            ]
            passed = sum(result.passed for result in matching)
            grouped[capability] = {
                "total": len(matching),
                "passed": passed,
                "failed": len(matching) - passed,
                "pass_rate": passed / len(matching),
            }
        return grouped


EffectivenessExecutor = Callable[
    [EffectivenessCase, int],
    Awaitable[Mapping[str, Any]],
]


class EffectivenessBenchmarkRunner:
    """Execute cases through injected adapters, then observe frozen checks.

    The runner owns no Agent completion authority. Each adapter may exercise a
    real product boundary; this class only compares returned facts.
    """

    def __init__(
        self,
        corpus: EffectivenessCorpus,
        *,
        executors: Mapping[EffectivenessExecutorKind, EffectivenessExecutor],
    ) -> None:
        self._corpus = corpus
        self._executors = dict(executors)

    async def run(
        self,
        *,
        tracks: Sequence[EffectivenessTrack] | None = None,
    ) -> EffectivenessReport:
        selected = set(tracks) if tracks is not None else None
        cases = tuple(
            case
            for case in self._corpus.cases
            if selected is None or case.track in selected
        )
        if not cases:
            raise ValueError("effectiveness benchmark selected no cases")
        started = perf_counter()
        results: list[EffectivenessCaseResult] = []
        for case in cases:
            for repetition in range(1, case.repetitions + 1):
                results.append(await self._run_case(case, repetition))
        return EffectivenessReport(
            corpus_version=self._corpus.manifest.corpus_version,
            corpus_status=self._corpus.manifest.status,
            results=tuple(results),
            duration_ms=(perf_counter() - started) * 1_000,
        )

    async def _run_case(
        self,
        case: EffectivenessCase,
        repetition: int,
    ) -> EffectivenessCaseResult:
        started = perf_counter()
        executor = self._executors.get(case.executor)
        if executor is None:
            return EffectivenessCaseResult(
                case_id=case.id,
                case_name_zh=case.name_zh,
                track=case.track,
                executor=case.executor,
                capabilities=case.capabilities,
                repetition=repetition,
                passed=False,
                duration_ms=(perf_counter() - started) * 1_000,
                failures=(f"缺少执行适配器：{case.executor.value}",),
                error_type="missing_executor",
            )
        try:
            observed = await executor(case, repetition)
            facts = dict(observed)
        except Exception as exc:  # noqa: BLE001 - benchmark failures are data
            return EffectivenessCaseResult(
                case_id=case.id,
                case_name_zh=case.name_zh,
                track=case.track,
                executor=case.executor,
                capabilities=case.capabilities,
                repetition=repetition,
                passed=False,
                duration_ms=(perf_counter() - started) * 1_000,
                failures=(f"执行器异常：{type(exc).__name__}",),
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
            )
        failures = _evaluate_checks(case.checks, facts)
        return EffectivenessCaseResult(
            case_id=case.id,
            case_name_zh=case.name_zh,
            track=case.track,
            executor=case.executor,
            capabilities=case.capabilities,
            repetition=repetition,
            passed=not failures,
            duration_ms=(perf_counter() - started) * 1_000,
            failures=failures,
            facts=facts,
        )


def default_effectiveness_manifest_path() -> Path:
    """Return the packaged Chinese pilot manifest."""

    return Path(__file__).with_name("corpora") / "effectiveness-v1.manifest.json"


def default_effectiveness_cases_path() -> Path:
    """Return the packaged Chinese pilot JSONL cases."""

    return Path(__file__).with_name("corpora") / "effectiveness-v1.cases.jsonl"


def load_effectiveness_corpus(
    manifest_path: Path | str | None = None,
    cases_path: Path | str | None = None,
) -> EffectivenessCorpus:
    """Load and strictly validate the manifest plus line-addressable cases."""

    manifest_source = (
        Path(manifest_path)
        if manifest_path is not None
        else default_effectiveness_manifest_path()
    )
    cases_source = (
        Path(cases_path)
        if cases_path is not None
        else default_effectiveness_cases_path()
    )
    manifest = _load_manifest(manifest_source)
    try:
        lines = cases_source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(
            f"cannot read effectiveness cases {cases_source}: {exc}"
        ) from exc
    cases: list[EffectivenessCase] = []
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"effectiveness cases JSONL line {line_number} is not valid JSON: {exc}"
            ) from exc
        try:
            cases.append(_parse_case(value))
        except ValueError as exc:
            raise ValueError(
                f"invalid effectiveness case at line {line_number}: {exc}"
            ) from exc
    return EffectivenessCorpus(manifest=manifest, cases=tuple(cases))


def _load_manifest(source: Path) -> EffectivenessManifest:
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read effectiveness manifest {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"effectiveness manifest is not valid JSON: {exc}") from exc
    data = _require_object_value(raw, "effectiveness manifest")
    _reject_unknown(
        data,
        {
            "schema_version",
            "corpus_version",
            "language",
            "status",
            "description_zh",
            "required_tracks",
            "minimum_cases_by_track",
            "required_capabilities",
        },
        "effectiveness manifest",
    )
    tracks_raw = _require_list(data, "required_tracks")
    try:
        tracks = tuple(
            EffectivenessTrack(_require_text_value(item)) for item in tracks_raw
        )
    except ValueError as exc:
        raise ValueError(f"invalid effectiveness track: {exc}") from exc
    minimum_raw = _require_object(data, "minimum_cases_by_track")
    minimum: dict[EffectivenessTrack, int] = {}
    for key, value in minimum_raw.items():
        try:
            track = EffectivenessTrack(key)
        except ValueError as exc:
            raise ValueError(f"invalid minimum track: {key}") from exc
        minimum[track] = _require_int_value(value)
    return EffectivenessManifest(
        schema_version=_require_int(data, "schema_version"),
        corpus_version=_require_text(data, "corpus_version"),
        language=_require_text(data, "language"),
        status=_require_text(data, "status"),
        description_zh=_require_text(data, "description_zh"),
        required_tracks=tracks,
        minimum_cases_by_track=minimum,
        required_capabilities=tuple(
            _require_text_value(item)
            for item in _require_list(data, "required_capabilities")
        ),
    )


def _parse_case(raw: Any) -> EffectivenessCase:
    data = _require_object_value(raw, "effectiveness case")
    _reject_unknown(
        data,
        {
            "schema_version",
            "corpus_version",
            "id",
            "track",
            "executor",
            "split",
            "name_zh",
            "scenario_zh",
            "repetitions",
            "input",
            "checks",
            "capabilities",
            "tags",
        },
        "effectiveness case",
    )
    checks = tuple(_parse_check(item) for item in _require_list(data, "checks"))
    tags_raw = data.get("tags", [])
    if not isinstance(tags_raw, list):
        raise ValueError("effectiveness case tags must be a list")
    capabilities_raw = _require_list(data, "capabilities")
    try:
        track = EffectivenessTrack(_require_text(data, "track"))
        executor = EffectivenessExecutorKind(_require_text(data, "executor"))
        split = EffectivenessSplit(_require_text(data, "split"))
    except ValueError as exc:
        raise ValueError(f"invalid effectiveness enum value: {exc}") from exc
    return EffectivenessCase(
        schema_version=_require_int(data, "schema_version"),
        corpus_version=_require_text(data, "corpus_version"),
        id=_require_text(data, "id"),
        track=track,
        executor=executor,
        split=split,
        name_zh=_require_text(data, "name_zh"),
        scenario_zh=_require_text(data, "scenario_zh"),
        repetitions=_require_int(data, "repetitions"),
        input=_require_object(data, "input"),
        checks=checks,
        capabilities=tuple(_require_text_value(item) for item in capabilities_raw),
        tags=tuple(_require_text_value(item) for item in tags_raw),
    )


def _parse_check(raw: Any) -> EffectivenessCheck:
    data = _require_object_value(raw, "effectiveness check")
    _reject_unknown(
        data,
        {"path", "operator", "value", "description_zh"},
        "effectiveness check",
    )
    if "value" not in data:
        raise ValueError("effectiveness check value is required")
    try:
        operator = CheckOperator(_require_text(data, "operator"))
    except ValueError as exc:
        raise ValueError(f"invalid effectiveness check operator: {exc}") from exc
    return EffectivenessCheck(
        path=_require_text(data, "path"),
        operator=operator,
        value=data["value"],
        description_zh=_require_text(data, "description_zh"),
    )


def _evaluate_checks(
    checks: tuple[EffectivenessCheck, ...],
    facts: Mapping[str, Any],
) -> tuple[str, ...]:
    failures: list[str] = []
    for check in checks:
        found, actual = _resolve_path(facts, check.path)
        if not found:
            failures.append(f"缺少观测字段 {check.path}：{check.description_zh}")
            continue
        if not _compare(actual, check.operator, check.value):
            failures.append(
                f"{check.description_zh}（{check.path} {check.operator.value} "
                f"{check.value!r}，实际为 {actual!r}）"
            )
    return tuple(failures)


def _resolve_path(data: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _compare(actual: Any, operator: CheckOperator, expected: Any) -> bool:
    if operator is CheckOperator.EQ:
        return actual == expected
    if operator is CheckOperator.NE:
        return actual != expected
    if operator in {
        CheckOperator.LT,
        CheckOperator.LTE,
        CheckOperator.GT,
        CheckOperator.GTE,
    }:
        if not _is_number(actual) or not _is_number(expected):
            return False
        if operator is CheckOperator.LT:
            return actual < expected
        if operator is CheckOperator.LTE:
            return actual <= expected
        if operator is CheckOperator.GT:
            return actual > expected
        return actual >= expected
    try:
        contains = expected in actual
    except TypeError:
        return False
    if operator is CheckOperator.CONTAINS:
        return bool(contains)
    return not contains


def _numeric_fact(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _aggregate_usage(
    results: Iterable[EffectivenessCaseResult],
) -> dict[str, int | float]:
    input_tokens = 0
    output_tokens = 0
    totals: list[int] = []
    for result in results:
        usage = result.facts.get("usage")
        if not isinstance(usage, Mapping):
            continue
        input_tokens += _numeric_fact(usage.get("input_tokens"))
        output_tokens += _numeric_fact(usage.get("output_tokens"))
        total_tokens = _numeric_fact(usage.get("total_tokens"))
        if total_tokens > 0:
            totals.append(total_tokens)
    totals.sort()
    total = sum(totals)
    p95_index = max(0, ceil(len(totals) * 0.95) - 1) if totals else 0
    return {
        "observed_calls": len(totals),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
        "average_total_tokens": total / len(totals) if totals else 0.0,
        "p95_total_tokens": totals[p95_index] if totals else 0,
        "max_total_tokens": totals[-1] if totals else 0,
    }


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _reject_unknown(
    data: Mapping[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    unknown = sorted(set(data).difference(allowed))
    if unknown:
        raise ValueError(f"{label} contains unsupported fields: {', '.join(unknown)}")


def _require_object(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return dict(value)


def _require_object_value(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _require_list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return list(value)


def _require_text(data: Mapping[str, Any], key: str) -> str:
    return _require_text_value(data.get(key))


def _require_text_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be a non-empty string")
    return value.strip()


def _require_int(data: Mapping[str, Any], key: str) -> int:
    return _require_int_value(data.get(key))


def _require_int_value(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("value must be an integer")
    return value
