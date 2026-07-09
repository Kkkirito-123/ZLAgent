"""JSON-plan planner for LLM-backed agents."""

from __future__ import annotations

import json
from typing import Any

from harness.model import ModelClient, ModelMessage
from harness.runtime import RuntimeAcceptanceInput, RuntimeToolStep
from harness.tasking import AcceptanceCriterion, CriterionType, TaskContract

from .planner import AgentPlan, AgentPlanner, AgentRunRequest


class PlanParseError(ValueError):
    """Raised when model output cannot become a valid AgentPlan."""


class JsonPlanPlanner(AgentPlanner):
    """Planner that asks a model for strict JSON and validates the result."""

    def __init__(self, model: ModelClient, *, system_prompt: str | None = None) -> None:
        self._model = model
        self._system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT

    async def plan(self, request: AgentRunRequest) -> AgentPlan:
        response = await self._model.complete((
            ModelMessage(role="system", content=self._system_prompt),
            ModelMessage(role="user", content=_request_prompt(request)),
        ))
        return parse_agent_plan(response.content, fallback_goal=request.user_goal)


def parse_agent_plan(text: str, *, fallback_goal: str) -> AgentPlan:
    """Parse and validate a model-produced JSON plan."""

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanParseError(f"plan is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanParseError("plan root must be a JSON object")

    contract = _parse_contract(data.get("contract"), fallback_goal=fallback_goal)
    steps = tuple(_parse_step(item) for item in _require_list(data, "steps"))
    acceptance = _parse_acceptance(data.get("acceptance") or {})
    return AgentPlan(contract=contract, steps=steps, acceptance=acceptance)


def _parse_contract(value: Any, *, fallback_goal: str) -> TaskContract:
    if not isinstance(value, dict):
        raise PlanParseError("contract must be an object")
    criteria_raw = _require_list(value, "acceptance_criteria")
    criteria = tuple(_parse_criterion(item) for item in criteria_raw)
    return TaskContract(
        id=_require_str(value, "id"),
        user_goal=str(value.get("user_goal") or fallback_goal),
        stakeholders=tuple(_as_str_list(value.get("stakeholders") or [])),
        mvp_scope=tuple(_as_str_list(value.get("mvp_scope") or [])),
        out_of_scope=tuple(_as_str_list(value.get("out_of_scope") or [])),
        acceptance_criteria=criteria,
        capability_boundaries=dict(value.get("capability_boundaries") or {}),
        freshness_policy=dict(value.get("freshness_policy") or {}),
        version=str(value.get("version") or "1"),
    )


def _parse_criterion(value: Any) -> AcceptanceCriterion:
    if not isinstance(value, dict):
        raise PlanParseError("acceptance criterion must be an object")
    try:
        criterion_type = CriterionType(_require_str(value, "type"))
    except ValueError as exc:
        raise PlanParseError(f"unknown criterion type: {value.get('type')}") from exc
    freshness = value.get("freshness_window_seconds")
    if freshness is not None:
        try:
            freshness = int(freshness)
        except (TypeError, ValueError) as exc:
            raise PlanParseError("freshness_window_seconds must be an integer") from exc
    return AcceptanceCriterion(
        id=_require_str(value, "id"),
        description=_require_str(value, "description"),
        type=criterion_type,
        required=bool(value.get("required", True)),
        evidence_refs=tuple(_as_str_list(value.get("evidence_refs") or [])),
        freshness_window_seconds=freshness,
        metadata=dict(value.get("metadata") or {}),
    )


def _parse_step(value: Any) -> RuntimeToolStep:
    if not isinstance(value, dict):
        raise PlanParseError("runtime step must be an object")
    return RuntimeToolStep(
        id=_require_str(value, "id"),
        tool_name=_require_str(value, "tool_name"),
        arguments=dict(value.get("arguments") or {}),
        allow_confirm=bool(value.get("allow_confirm", False)),
        title=value.get("title"),
        expected_output=str(value.get("expected_output") or ""),
        verification=str(value.get("verification") or ""),
        depends_on=tuple(_as_str_list(value.get("depends_on") or [])),
        required_evidence_refs=tuple(_as_str_list(value.get("required_evidence_refs") or [])),
    )


def _parse_acceptance(value: Any) -> RuntimeAcceptanceInput:
    if not isinstance(value, dict):
        raise PlanParseError("acceptance must be an object")
    freshness_by_ref = value.get("freshness_by_ref") or {}
    if freshness_by_ref:
        raise PlanParseError(
            "freshness_by_ref cannot be supplied by JSON planner yet; "
            "freshness must come from trusted runtime tools"
        )
    return RuntimeAcceptanceInput(
        evidence_refs=tuple(_as_str_list(value.get("evidence_refs") or [])),
        passed_tests=tuple(_as_str_list(value.get("passed_tests") or [])),
        human_approvals=tuple(_as_str_list(value.get("human_approvals") or [])),
    )


def _require_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise PlanParseError(f"{key} must be a list")
    return value


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlanParseError(f"{key} must be a non-empty string")
    return value.strip()


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise PlanParseError("expected a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PlanParseError("expected a list of strings")
        if item.strip():
            out.append(item.strip())
    return out


def _request_prompt(request: AgentRunRequest) -> str:
    return (
        "Create a JSON agent plan for this request.\n"
        f"run_id: {request.run_id}\n"
        f"user_goal: {request.user_goal}\n"
        "Return JSON only."
    )


_DEFAULT_SYSTEM_PROMPT = (
    "You are a planner. Return a strict JSON object with keys: "
    "contract, steps, acceptance. Do not execute tools."
)
