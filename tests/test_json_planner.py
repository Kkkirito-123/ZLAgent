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
    def __init__(self, content: str, *, raw: dict | None = None) -> None:
        self.content = content
        self.raw = dict(raw or {})
        self.messages: tuple[ModelMessage, ...] = ()

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        self.messages = messages
        return ModelResponse(content=self.content, raw=self.raw)


class SequenceModel:
    def __init__(self, *contents: str) -> None:
        self._contents = list(contents)
        self.calls: list[tuple[ModelMessage, ...]] = []

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        self.calls.append(messages)
        if not self._contents:
            raise AssertionError("planner exceeded the supplied model responses")
        return ModelResponse(content=self._contents.pop(0))


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
    }
    data.update(overrides)
    return json.dumps(data)


class JsonPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_planner_calls_model_and_returns_agent_plan(self) -> None:
        model = FakeModel(
            _plan_json(),
            raw={"provider": "test", "model": "planner", "usage": {"input": 1}},
        )
        planner = JsonPlanPlanner(
            model,
            tool_schemas=[
                {
                    "name": "read_file",
                    "input_schema": {"type": "object"},
                    "permission": "safe",
                }
            ],
        )

        plan = await planner.plan(
            AgentRunRequest(
                run_id="run-1",
                user_goal="ship plan",
                context={"channel": "cli"},
            )
        )

        self.assertEqual(plan.contract.id, "contract_run-1")
        self.assertEqual(plan.contract.user_goal, "ship plan")
        self.assertEqual(
            plan.contract.acceptance_criteria[0].type, CriterionType.TOOL_EVIDENCE
        )
        self.assertEqual(plan.steps[0].tool_name, "read_file")
        self.assertFalse(hasattr(plan, "acceptance"))
        self.assertEqual(plan.metadata["proposed_contract_id"], "contract-1")
        self.assertEqual(plan.metadata["provider"], "test")
        self.assertEqual(model.messages[0].role, "system")
        self.assertEqual(model.messages[1].role, "user")
        self.assertIn('"channel": "cli"', model.messages[1].content)
        self.assertIn('"name": "read_file"', model.messages[1].content)

    async def test_planner_repairs_schema_invalid_arguments_once(self) -> None:
        invalid = json.loads(_plan_json())
        invalid["steps"][0]["arguments"] = {"path": 3}
        model = SequenceModel(json.dumps(invalid), _plan_json())
        planner = JsonPlanPlanner(
            model,
            tool_schemas=[
                {
                    "name": "read_file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ],
        )

        plan = await planner.plan(
            AgentRunRequest(run_id="run-1", user_goal="ship plan")
        )

        self.assertEqual(plan.steps[0].arguments, {"path": "README.md"})
        self.assertEqual(plan.metadata["plan_attempts"], 2)
        self.assertEqual(plan.metadata["repair_attempts"], 1)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(
            tuple(message.role for message in model.calls[1]),
            ("system", "user", "assistant", "user"),
        )
        self.assertIn("$.path", model.calls[1][-1].content)

    async def test_planner_stops_after_bounded_repair_attempt(self) -> None:
        invalid = json.loads(_plan_json())
        invalid["steps"][0]["arguments"] = {}
        bad_plan = json.dumps(invalid)
        model = SequenceModel(bad_plan, bad_plan)
        planner = JsonPlanPlanner(
            model,
            tool_schemas=[
                {
                    "name": "read_file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ],
            max_repair_attempts=1,
        )

        with self.assertRaisesRegex(PlanParseError, "required property"):
            await planner.plan(
                AgentRunRequest(run_id="run-1", user_goal="ship plan")
            )

        self.assertEqual(len(model.calls), 2)

    async def test_planner_rejects_identical_repeated_actions(self) -> None:
        repeated = json.loads(_plan_json())
        repeated["steps"].append(
            {
                "id": "step-2",
                "tool_name": "read_file",
                "arguments": {"path": "README.md"},
            }
        )
        planner = JsonPlanPlanner(
            FakeModel(json.dumps(repeated)),
            tool_schemas=[
                {
                    "name": "read_file",
                    "input_schema": {"type": "object"},
                }
            ],
            max_repair_attempts=0,
        )

        with self.assertRaisesRegex(PlanParseError, "identical tool action"):
            await planner.plan(
                AgentRunRequest(run_id="run-1", user_goal="ship plan")
            )

    async def test_planner_rejects_plan_above_step_limit(self) -> None:
        oversized = json.loads(_plan_json())
        oversized["steps"].append(
            {
                "id": "step-2",
                "tool_name": "read_file",
                "arguments": {"path": "CLAUDE.md"},
            }
        )
        planner = JsonPlanPlanner(
            FakeModel(json.dumps(oversized)),
            tool_schemas=[
                {
                    "name": "read_file",
                    "input_schema": {"type": "object"},
                }
            ],
            max_repair_attempts=0,
            max_plan_steps=1,
        )

        with self.assertRaisesRegex(PlanParseError, "maximum is 1"):
            await planner.plan(
                AgentRunRequest(run_id="run-1", user_goal="ship plan")
            )

    async def test_planner_rejects_tool_outside_host_schema(self) -> None:
        model = FakeModel(_plan_json())
        planner = JsonPlanPlanner(model, tool_schemas=[])

        with self.assertRaisesRegex(PlanParseError, "unavailable tools"):
            await planner.plan(AgentRunRequest(run_id="run-1", user_goal="ship plan"))

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
                json.dumps({"contract": {"id": "c"}, "steps": []}),
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

    def test_parse_rejects_all_llm_supplied_acceptance_facts(self) -> None:
        payloads = (
            {"evidence_refs": ["model:claim"]},
            {"passed_tests": ["unit-tests"]},
            {"human_approvals": ["approval"]},
            {"freshness_by_ref": {"web:source": "2026-07-09T00:00:00Z"}},
            {},
        )
        for acceptance in payloads:
            with self.subTest(acceptance=acceptance):
                bad = json.loads(_plan_json())
                bad["acceptance"] = acceptance
                with self.assertRaises(PlanParseError):
                    parse_agent_plan(json.dumps(bad), fallback_goal="goal")

    def test_parse_rejects_model_granted_confirmation(self) -> None:
        bad = json.loads(_plan_json())
        bad["steps"][0]["allow_confirm"] = True

        with self.assertRaisesRegex(PlanParseError, "cannot grant confirmation"):
            parse_agent_plan(json.dumps(bad), fallback_goal="goal")

    def test_parse_rejects_values_that_need_implicit_type_coercion(self) -> None:
        malformed = []
        bad_required = json.loads(_plan_json())
        bad_required["contract"]["acceptance_criteria"][0]["required"] = "false"
        malformed.append(bad_required)
        bad_arguments = json.loads(_plan_json())
        bad_arguments["steps"][0]["arguments"] = []
        malformed.append(bad_arguments)

        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(PlanParseError):
                    parse_agent_plan(json.dumps(payload), fallback_goal="goal")


if __name__ == "__main__":
    unittest.main()
