from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.agent import (  # noqa: E402
    AgentRunRequest,
    JsonPlanPlanner,
    PlanParseError,
    parse_agent_plan,
)
from re_zlagent.harness.model import ModelMessage, ModelResponse  # noqa: E402
from re_zlagent.harness.tasking import CriterionType  # noqa: E402


class FakeModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: tuple[ModelMessage, ...] = ()

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        self.messages = messages
        return ModelResponse(content=self.content)


def _plan_json(**overrides) -> str:
    data = {
        "contract": {
            "id": "contract-1",
            "user_goal": "ship plan",
            "acceptance_criteria": [
                {
                    "id": "evidence",
                    "description": "evidence exists",
                    "type": "tool_evidence",
                    "evidence_refs": ["tool:evidence"],
                }
            ],
        },
        "steps": [
            {
                "id": "step-1",
                "tool_name": "read_file",
                "arguments": {"path": "README.md"},
            }
        ],
        "acceptance": {
            "evidence_refs": ["manual:evidence"],
            "passed_tests": [],
            "human_approvals": [],
        },
    }
    data.update(overrides)
    return json.dumps(data)


class JsonPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_planner_calls_model_and_returns_agent_plan(self) -> None:
        model = FakeModel(_plan_json())
        planner = JsonPlanPlanner(model)

        plan = await planner.plan(
            AgentRunRequest(run_id="run-1", user_goal="ship plan")
        )

        self.assertEqual(plan.contract.id, "contract-1")
        self.assertEqual(plan.contract.acceptance_criteria[0].type, CriterionType.TOOL_EVIDENCE)
        self.assertEqual(plan.steps[0].tool_name, "read_file")
        self.assertEqual(plan.acceptance.evidence_refs, ("manual:evidence",))
        self.assertEqual(model.messages[0].role, "system")
        self.assertEqual(model.messages[1].role, "user")

    def test_parse_step_metadata_and_dependencies(self) -> None:
        data = json.loads(_plan_json())
        data["steps"] = [
            {
                "id": "step-1",
                "title": "read",
                "tool_name": "read_file",
                "expected_output": "file content",
                "verification": "file evidence",
                "required_evidence_refs": ["file:read"],
            },
            {
                "id": "step-2",
                "title": "write",
                "tool_name": "write_file",
                "depends_on": ["step-1"],
            },
        ]

        plan = parse_agent_plan(json.dumps(data), fallback_goal="goal")

        self.assertEqual(plan.steps[0].title, "read")
        self.assertEqual(plan.steps[0].required_evidence_refs, ("file:read",))
        self.assertEqual(plan.steps[1].depends_on, ("step-1",))

    def test_parse_rejects_out_of_order_dependencies(self) -> None:
        data = json.loads(_plan_json())
        data["steps"] = [
            {
                "id": "step-2",
                "tool_name": "write_file",
                "depends_on": ["step-1"],
            },
            {
                "id": "step-1",
                "tool_name": "read_file",
            },
        ]

        with self.assertRaises(ValueError):
            parse_agent_plan(json.dumps(data), fallback_goal="goal")

    async def test_model_message_validates_role(self) -> None:
        with self.assertRaises(ValueError):
            ModelMessage(role="tool", content="bad")

    def test_parse_rejects_invalid_json(self) -> None:
        with self.assertRaises(PlanParseError):
            parse_agent_plan("{not-json", fallback_goal="goal")

    def test_parse_rejects_missing_contract_list_fields(self) -> None:
        with self.assertRaises(PlanParseError):
            parse_agent_plan(
                json.dumps({"contract": {"id": "c"}, "steps": [], "acceptance": {}}),
                fallback_goal="goal",
            )

    def test_parse_rejects_unknown_criterion_type(self) -> None:
        bad = json.loads(_plan_json())
        bad["contract"]["acceptance_criteria"][0]["type"] = "made_up"

        with self.assertRaises(PlanParseError):
            parse_agent_plan(json.dumps(bad), fallback_goal="goal")

    def test_parse_rejects_duplicate_step_ids_via_agent_plan(self) -> None:
        bad = json.loads(_plan_json())
        bad["steps"].append(dict(bad["steps"][0]))

        with self.assertRaises(ValueError):
            parse_agent_plan(json.dumps(bad), fallback_goal="goal")

    def test_parse_rejects_llm_supplied_freshness_timestamps(self) -> None:
        bad = json.loads(_plan_json())
        bad["acceptance"]["freshness_by_ref"] = {"web:source": "2026-07-09T00:00:00Z"}

        with self.assertRaises(PlanParseError):
            parse_agent_plan(json.dumps(bad), fallback_goal="goal")


if __name__ == "__main__":
    unittest.main()
