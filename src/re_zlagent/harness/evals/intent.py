"""Versioned intent-routing corpus and accuracy evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from re_zlagent.harness.agent import (
    AgentRunRequest,
    IntentReasonCode,
    IntentRoute,
    IntentRouter,
)


INTENT_CORPUS_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class IntentEvalCase:
    """One labeled top-level routing example."""

    id: str
    name: str
    user_input: str
    expected_route: IntentRoute
    language: str
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.id, "intent case id"),
            (self.name, "intent case name"),
            (self.user_input, "intent case user_input"),
            (self.language, "intent case language"),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        object.__setattr__(self, "context", dict(self.context))


@dataclass(frozen=True, slots=True)
class IntentEvalCorpus:
    """Validated versioned seed corpus."""

    schema_version: int
    corpus_version: str
    minimum_accuracy: float
    cases: tuple[IntentEvalCase, ...]

    def __post_init__(self) -> None:
        if self.schema_version != INTENT_CORPUS_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported intent corpus schema version: {self.schema_version}"
            )
        if not self.corpus_version.strip():
            raise ValueError("intent corpus_version must be non-empty")
        if self.minimum_accuracy < 0 or self.minimum_accuracy > 1:
            raise ValueError("intent minimum_accuracy must be between 0 and 1")
        object.__setattr__(self, "cases", tuple(self.cases))
        if not self.cases:
            raise ValueError("intent corpus must contain cases")
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("intent case ids must be unique")
        present_routes = {case.expected_route for case in self.cases}
        missing = sorted(route.value for route in set(IntentRoute) - present_routes)
        if missing:
            raise ValueError(
                "intent corpus is missing routes: " + ", ".join(missing)
            )


@dataclass(frozen=True, slots=True)
class IntentEvalCaseResult:
    """Observed routing result for one labeled case."""

    case_id: str
    case_name: str
    language: str
    expected_route: IntentRoute
    predicted_route: IntentRoute | None
    correct: bool
    duration_ms: float
    reason_code: IntentReasonCode | None = None
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("intent case duration must be >= 0")
        if self.correct and self.predicted_route is not self.expected_route:
            raise ValueError("correct intent result must match expected route")
        if self.predicted_route is None and self.error_type is None:
            raise ValueError("missing prediction requires an error type")
        if self.error_message is not None:
            object.__setattr__(
                self,
                "error_message",
                self.error_message.strip()[:300],
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_name": self.case_name,
            "language": self.language,
            "expected_route": self.expected_route.value,
            "predicted_route": (
                self.predicted_route.value
                if self.predicted_route is not None
                else None
            ),
            "correct": self.correct,
            "duration_ms": round(self.duration_ms, 3),
            "reason_code": (
                self.reason_code.value if self.reason_code is not None else None
            ),
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class IntentEvalReport:
    """Aggregate accuracy, confusion, latency, and usage report."""

    corpus_version: str
    minimum_accuracy: float
    results: tuple[IntentEvalCaseResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(self.results))
        if not self.results:
            raise ValueError("intent eval report must contain results")

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def correct_count(self) -> int:
        return sum(result.correct for result in self.results)

    @property
    def invalid_count(self) -> int:
        return sum(result.predicted_route is None for result in self.results)

    @property
    def accuracy(self) -> float:
        return self.correct_count / self.total

    @property
    def average_latency_ms(self) -> float:
        return sum(result.duration_ms for result in self.results) / self.total

    @property
    def ok(self) -> bool:
        return self.invalid_count == 0 and self.accuracy >= self.minimum_accuracy

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "corpus_version": self.corpus_version,
            "minimum_accuracy": self.minimum_accuracy,
            "aggregate": {
                "total": self.total,
                "correct": self.correct_count,
                "incorrect": self.total - self.correct_count,
                "invalid": self.invalid_count,
                "accuracy": self.accuracy,
                "average_latency_ms": round(self.average_latency_ms, 3),
                "usage": self._aggregate_usage(),
            },
            "by_route": self._by_route(),
            "by_language": self._by_language(),
            "confusion_matrix": self._confusion_matrix(),
            "model": self._model_metadata(),
            "results": [result.to_dict() for result in self.results],
        }

    def _by_route(self) -> dict[str, dict[str, int | float]]:
        grouped: dict[str, dict[str, int | float]] = {}
        for route in IntentRoute:
            matching = [
                result
                for result in self.results
                if result.expected_route is route
            ]
            correct = sum(result.correct for result in matching)
            grouped[route.value] = {
                "total": len(matching),
                "correct": correct,
                "accuracy": correct / len(matching) if matching else 0.0,
            }
        return grouped

    def _by_language(self) -> dict[str, dict[str, int | float]]:
        grouped: dict[str, dict[str, int | float]] = {}
        for language in sorted({result.language for result in self.results}):
            matching = [
                result for result in self.results if result.language == language
            ]
            correct = sum(result.correct for result in matching)
            grouped[language] = {
                "total": len(matching),
                "correct": correct,
                "accuracy": correct / len(matching) if matching else 0.0,
            }
        return grouped

    def _confusion_matrix(self) -> dict[str, dict[str, int]]:
        columns = [route.value for route in IntentRoute] + ["invalid"]
        matrix = {
            expected.value: dict.fromkeys(columns, 0)
            for expected in IntentRoute
        }
        for result in self.results:
            predicted = (
                result.predicted_route.value
                if result.predicted_route is not None
                else "invalid"
            )
            matrix[result.expected_route.value][predicted] += 1
        return matrix

    def _aggregate_usage(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for result in self.results:
            usage = result.metadata.get("usage")
            if not isinstance(usage, dict):
                continue
            for key, value in usage.items():
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
                totals[key] = totals.get(key, 0) + value
        return totals

    def _model_metadata(self) -> dict[str, list[str]]:
        providers: set[str] = set()
        models: set[str] = set()
        for result in self.results:
            provider = result.metadata.get("provider")
            model = result.metadata.get("model")
            if isinstance(provider, str):
                providers.add(provider)
            if isinstance(model, str):
                models.add(model)
        return {
            "providers": sorted(providers),
            "models": sorted(models),
        }


class IntentEvalRunner:
    """Evaluate an IntentRouter sequentially against a labeled corpus."""

    def __init__(
        self,
        router: IntentRouter,
        corpus: IntentEvalCorpus,
    ) -> None:
        self._router = router
        self._corpus = corpus

    async def run(self) -> IntentEvalReport:
        results: list[IntentEvalCaseResult] = []
        for case in self._corpus.cases:
            started = perf_counter()
            try:
                decision = await self._router.route(
                    AgentRunRequest(
                        run_id=f"intent-eval-{case.id}",
                        user_goal=case.user_input,
                        context=case.context,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - invalid routes are eval data
                results.append(
                    IntentEvalCaseResult(
                        case_id=case.id,
                        case_name=case.name,
                        language=case.language,
                        expected_route=case.expected_route,
                        predicted_route=None,
                        correct=False,
                        duration_ms=(perf_counter() - started) * 1_000,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
                continue
            results.append(
                IntentEvalCaseResult(
                    case_id=case.id,
                    case_name=case.name,
                    language=case.language,
                    expected_route=case.expected_route,
                    predicted_route=decision.route,
                    correct=decision.route is case.expected_route,
                    duration_ms=(perf_counter() - started) * 1_000,
                    reason_code=decision.reason_code,
                    metadata=decision.metadata,
                )
            )
        return IntentEvalReport(
            corpus_version=self._corpus.corpus_version,
            minimum_accuracy=self._corpus.minimum_accuracy,
            results=tuple(results),
        )


def default_intent_corpus_path() -> Path:
    """Return the seed intent corpus shipped with the package."""

    return Path(__file__).with_name("corpora") / "intent-routing-v1.json"


def default_intent_stress_corpus_path() -> Path:
    """Return the packaged mixed and ambiguous routing corpus."""

    return (
        Path(__file__).with_name("corpora")
        / "intent-routing-stress-v1.json"
    )


def load_intent_eval_corpus(
    path: Path | str | None = None,
) -> IntentEvalCorpus:
    """Load and strictly validate a labeled intent corpus."""

    source = Path(path) if path is not None else default_intent_corpus_path()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read intent corpus {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"intent corpus is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("intent corpus root must be an object")

    unknown = sorted(
        set(raw).difference(
            {"schema_version", "corpus_version", "minimum_accuracy", "cases"}
        )
    )
    if unknown:
        raise ValueError(
            "intent corpus contains unsupported fields: " + ", ".join(unknown)
        )
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list):
        raise ValueError("intent corpus cases must be a list")
    return IntentEvalCorpus(
        schema_version=_require_int(raw, "schema_version"),
        corpus_version=_require_text(raw, "corpus_version"),
        minimum_accuracy=_require_number(raw, "minimum_accuracy"),
        cases=tuple(_parse_case(item) for item in cases_raw),
    )


def _parse_case(value: Any) -> IntentEvalCase:
    if not isinstance(value, dict):
        raise ValueError("intent corpus case must be an object")
    unknown = sorted(
        set(value).difference(
            {"id", "name", "user_input", "expected_route", "language", "context"}
        )
    )
    if unknown:
        raise ValueError(
            "intent case contains unsupported fields: " + ", ".join(unknown)
        )
    context = value.get("context", {})
    if not isinstance(context, dict):
        raise ValueError("intent case context must be an object")
    try:
        expected_route = IntentRoute(_require_text(value, "expected_route"))
    except ValueError as exc:
        raise ValueError(
            f"invalid intent expected route: {value.get('expected_route')}"
        ) from exc
    return IntentEvalCase(
        id=_require_text(value, "id"),
        name=_require_text(value, "name"),
        user_input=_require_text(value, "user_input"),
        expected_route=expected_route,
        language=_require_text(value, "language"),
        context=context,
    )


def _require_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _require_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _require_number(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    return float(value)
