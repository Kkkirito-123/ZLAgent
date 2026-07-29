from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.agent import (  # noqa: E402
    AgentOrchestrator,
    AgentPlan,
    AgentRunRequest,
    GeneralAgent,
    GeneralAgentMode,
    GeneralAgentUnavailableError,
    IntentDecision,
    IntentReasonCode,
    IntentRoute,
    StaticAgentPlanner,
)
from re_zlagent.harness.model import ModelMessage, ModelResponse  # noqa: E402
from re_zlagent.harness.memory import InMemoryMemoryStore, MemoryManager  # noqa: E402
from re_zlagent.harness.runtime import HarnessRuntime, RuntimeToolStep  # noqa: E402
from re_zlagent.harness.storage import InMemoryTaskStore  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    TaskContract,
    TaskRunStatus,
)
from re_zlagent.harness.tools import (  # noqa: E402
    Evidence,
    Tool,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)


class GeneralEvidenceTool(Tool):
    name = "general_evidence"
    description = "Return bounded evidence for general agent tests."
    permission = ToolPermission.SAFE

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.success(
            str(arguments.get("content") or "runtime answer"),
            evidence=[Evidence(type="general_test", ref="general:evidence")],
            source=self.name,
        )


class RecordingModel:
    def __init__(self, *responses: ModelResponse | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[ModelMessage, ...]] = []

    async def complete(
        self,
        messages: tuple[ModelMessage, ...],
    ) -> ModelResponse:
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("unexpected model call")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StaticIntentRouter:
    def __init__(self, decision: IntentDecision) -> None:
        self.decision = decision
        self.calls: list[AgentRunRequest] = []

    async def route(self, request: AgentRunRequest) -> IntentDecision:
        self.calls.append(request)
        return self.decision


class GeneralAgentTests(unittest.IsolatedAsyncioTestCase):
    def _runtime_graph(
        self,
        *,
        evidence_ref: str = "general:evidence",
        content: str = "runtime answer",
    ) -> tuple[AgentOrchestrator, InMemoryTaskStore]:
        store = InMemoryTaskStore()
        tools = ToolRegistry()
        tools.register(GeneralEvidenceTool())
        runtime = HarnessRuntime(store=store, tools=tools)
        plan = AgentPlan(
            contract=TaskContract(
                id="contract-general",
                user_goal="general task",
                acceptance_criteria=(
                    AcceptanceCriterion(
                        id="has-evidence",
                        description="runtime evidence exists",
                        type=CriterionType.TOOL_EVIDENCE,
                        evidence_refs=(evidence_ref,),
                    ),
                ),
            ),
            steps=(
                RuntimeToolStep(
                    id="step-general",
                    tool_name="general_evidence",
                    arguments={"content": content},
                ),
            ),
        )
        return (
            AgentOrchestrator(
                planner=StaticAgentPlanner(plan),
                runtime=runtime,
            ),
            store,
        )

    async def test_chat_uses_model_without_creating_task_run(self) -> None:
        orchestrator, store = self._runtime_graph()
        model = RecordingModel(
            ModelResponse(
                content="你好，我是通用助手。",
                raw={"provider": "test", "usage": {"total_tokens": 12}},
            )
        )
        agent = GeneralAgent(
            orchestrator=orchestrator,
            response_model=model,
            max_context_chars=256,
        )
        request = AgentRunRequest(
            run_id="chat-1",
            user_goal="你好",
            context={"large": "x" * 600},
        )

        result = await agent.run(request, mode=GeneralAgentMode.CHAT)

        self.assertEqual(result.response, "你好，我是通用助手。")
        self.assertFalse(result.verified)
        self.assertIsNone(result.agent_result)
        self.assertIsNone(store.get_run("chat-1"))
        self.assertEqual(result.response_metadata["provider"], "test")
        self.assertIn("[...truncated by host...]", model.calls[0][1].content)

    async def test_chat_requires_response_model(self) -> None:
        orchestrator, _ = self._runtime_graph()
        agent = GeneralAgent(orchestrator=orchestrator)

        with self.assertRaises(GeneralAgentUnavailableError):
            await agent.run(
                AgentRunRequest(run_id="chat-1", user_goal="hello"),
                mode="chat",
            )

    async def test_task_preserves_runtime_acceptance_then_synthesizes(self) -> None:
        orchestrator, store = self._runtime_graph(content="trusted runtime output")
        model = RecordingModel(
            ModelResponse(
                content="已根据运行结果完成处理。",
                raw={"model": "response-model"},
            )
        )
        agent = GeneralAgent(
            orchestrator=orchestrator,
            response_model=model,
        )

        result = await agent.run(
            AgentRunRequest(run_id="task-1", user_goal="execute task"),
            mode="task",
        )

        self.assertTrue(result.verified)
        self.assertTrue(result.agent_result.accepted)
        self.assertEqual(result.response, "已根据运行结果完成处理。")
        self.assertEqual(store.get_run("task-1").status, TaskRunStatus.COMPLETED)
        self.assertIn("trusted runtime output", model.calls[0][1].content)
        self.assertEqual(result.response_metadata["model"], "response-model")

    async def test_task_without_response_model_returns_runtime_output(self) -> None:
        orchestrator, _ = self._runtime_graph(content="plain runtime output")
        agent = GeneralAgent(orchestrator=orchestrator)

        result = await agent.run(
            AgentRunRequest(run_id="task-1", user_goal="execute task"),
        )

        self.assertTrue(result.verified)
        self.assertEqual(result.response, "plain runtime output")
        self.assertEqual(result.response_metadata["response_source"], "runtime")

    async def test_response_model_failure_cannot_change_task_truth(self) -> None:
        orchestrator, store = self._runtime_graph(content="fallback output")
        model = RecordingModel(RuntimeError("response provider unavailable"))
        agent = GeneralAgent(
            orchestrator=orchestrator,
            response_model=model,
        )

        result = await agent.run(
            AgentRunRequest(run_id="task-1", user_goal="execute task"),
        )

        self.assertTrue(result.verified)
        self.assertEqual(result.response, "fallback output")
        self.assertEqual(store.get_run("task-1").status, TaskRunStatus.COMPLETED)
        self.assertEqual(
            result.response_metadata["response_source"],
            "runtime_fallback",
        )
        self.assertEqual(
            result.response_metadata["response_error_type"],
            "RuntimeError",
        )

    async def test_failed_acceptance_skips_response_model(self) -> None:
        orchestrator, _ = self._runtime_graph(evidence_ref="missing:evidence")
        model = RecordingModel(
            ModelResponse(content="must not be used"),
        )
        agent = GeneralAgent(
            orchestrator=orchestrator,
            response_model=model,
        )

        result = await agent.run(
            AgentRunRequest(run_id="task-1", user_goal="execute task"),
        )

        self.assertFalse(result.verified)
        self.assertEqual(result.response, "任务未通过验收。")
        self.assertEqual(model.calls, [])

    async def test_auto_chat_routes_without_creating_task_truth(self) -> None:
        orchestrator, store = self._runtime_graph()
        router = StaticIntentRouter(
            IntentDecision(
                route=IntentRoute.CHAT,
                reason_code=IntentReasonCode.DIRECT_ANSWER,
            )
        )
        model = RecordingModel(ModelResponse(content="自动聊天回答"))
        agent = GeneralAgent(
            orchestrator=orchestrator,
            response_model=model,
            intent_router=router,
        )

        result = await agent.run(
            AgentRunRequest(run_id="auto-chat", user_goal="解释 checkpoint"),
            mode="auto",
        )

        self.assertEqual(result.mode, GeneralAgentMode.CHAT)
        self.assertEqual(result.response, "自动聊天回答")
        self.assertFalse(result.verified)
        self.assertIsNone(store.get_run("auto-chat"))
        self.assertEqual(result.intent_decision.route, IntentRoute.CHAT)
        self.assertIsNotNone(result.context_manifest)

    async def test_auto_chat_aggregates_router_and_response_usage(self) -> None:
        orchestrator, _ = self._runtime_graph()
        router = StaticIntentRouter(
            IntentDecision(
                route=IntentRoute.CHAT,
                reason_code=IntentReasonCode.DIRECT_ANSWER,
                metadata={
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    }
                },
            )
        )
        model = RecordingModel(
            ModelResponse(
                content="回答",
                raw={
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 5,
                        "total_tokens": 25,
                    }
                },
            )
        )
        agent = GeneralAgent(
            orchestrator=orchestrator,
            response_model=model,
            intent_router=router,
        )

        result = await agent.run(
            AgentRunRequest(run_id="auto-usage", user_goal="解释"),
            mode="auto",
        )

        self.assertEqual(
            result.token_usage["aggregate"],
            {"input_tokens": 30, "output_tokens": 7, "total_tokens": 37},
        )
        self.assertEqual(
            set(result.token_usage["phases"]),
            {"router", "response"},
        )

    async def test_auto_clarify_and_gated_task_do_not_execute(self) -> None:
        for run_id, decision, expected_mode in (
            (
                "auto-clarify",
                IntentDecision(
                    route=IntentRoute.CLARIFY,
                    reason_code=IntentReasonCode.MISSING_TARGET,
                    clarification_question="要处理哪个文件？",
                ),
                GeneralAgentMode.CLARIFY,
            ),
            (
                "auto-task-gated",
                IntentDecision(
                    route=IntentRoute.TASK,
                    reason_code=IntentReasonCode.EXTERNAL_READ,
                ),
                GeneralAgentMode.TASK,
            ),
        ):
            with self.subTest(run_id=run_id):
                orchestrator, store = self._runtime_graph()
                agent = GeneralAgent(
                    orchestrator=orchestrator,
                    intent_router=StaticIntentRouter(decision),
                )

                result = await agent.run(
                    AgentRunRequest(run_id=run_id, user_goal="处理它"),
                    mode="auto",
                )

                self.assertEqual(result.mode, expected_mode)
                self.assertFalse(result.verified)
                self.assertIsNone(store.get_run(run_id))

    async def test_host_can_enable_auto_task_through_same_runtime(self) -> None:
        orchestrator, store = self._runtime_graph(content="auto runtime")
        agent = GeneralAgent(
            orchestrator=orchestrator,
            intent_router=StaticIntentRouter(
                IntentDecision(
                    route=IntentRoute.TASK,
                    reason_code=IntentReasonCode.EXTERNAL_READ,
                )
            ),
            allow_auto_task_execution=True,
        )

        result = await agent.run(
            AgentRunRequest(run_id="auto-task", user_goal="读取资料"),
            mode="auto",
        )

        self.assertTrue(result.verified)
        self.assertEqual(result.response, "auto runtime")
        self.assertEqual(
            store.get_run("auto-task").status,
            TaskRunStatus.COMPLETED,
        )

    async def test_explicit_memory_write_is_recalled_through_manifest(self) -> None:
        orchestrator, _ = self._runtime_graph()
        memory = MemoryManager(InMemoryMemoryStore())
        model = RecordingModel(ModelResponse(content="会保持简洁。"))
        agent = GeneralAgent(
            orchestrator=orchestrator,
            response_model=model,
            memory_manager=memory,
        )

        remembered = await agent.run(
            AgentRunRequest(
                run_id="remember",
                user_goal="请记住：我喜欢简洁的报告。",
            ),
            mode="chat",
        )
        answered = await agent.run(
            AgentRunRequest(
                run_id="recall",
                user_goal="以后报告怎么写？",
            ),
            mode="chat",
        )

        self.assertEqual(remembered.response, "已记住。")
        self.assertTrue(remembered.memory_capture.written)
        self.assertIn("我喜欢简洁的报告", model.calls[0][1].content)
        manifest = answered.context_manifest.to_dict()
        self.assertIn(
            "recalled_memory",
            [segment["id"] for segment in manifest["segments"]],
        )
        self.assertNotIn("我喜欢简洁的报告", str(manifest))


if __name__ == "__main__":
    unittest.main()
