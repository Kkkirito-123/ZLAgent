from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.app import (  # noqa: E402
    ApplicationBootstrapConfig,
    build_application_runtime,
)
from re_zlagent.harness.mcp import (  # noqa: E402
    McpCallError,
    McpCallErrorCode,
    McpConfigurationError,
    McpToolDescriptor,
    create_mcp_tools,
    load_mcp_config,
    local_mcp_tool_name,
)
from re_zlagent.harness.runtime import RuntimeToolStep  # noqa: E402
from re_zlagent.harness.tasking import (  # noqa: E402
    AcceptanceCriterion,
    CriterionType,
    SideEffectStatus,
    TaskContract,
    TaskRunStatus,
)
from re_zlagent.harness.tools import (  # noqa: E402
    ToolErrorType,
    ToolRegistry,
    ToolResultStatus,
)


_MOCK_SERVER = r"""
import json
import os
import sys
import time

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "zlagent-test", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo text through the MCP boundary.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "hidden",
                    "description": "Must not enter the Harness registry.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "slow",
                    "description": "Respond after the configured deadline.",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
        }
    elif method == "tools/call":
        params = message["params"]
        if params["name"] == "slow":
            time.sleep(2.5)
            result = {
                "content": [{"type": "text", "text": "late"}],
                "isError": False,
            }
        elif params["name"] != "echo":
            result = {
                "content": [{"type": "text", "text": "unknown tool"}],
                "isError": True,
            }
        else:
            text = params.get("arguments", {}).get("text", "")
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": text + "|env:" + str(bool(os.environ.get("MCP_TOKEN"))),
                    }
                ],
                "structuredContent": {"echo": text},
                "isError": False,
            }
    else:
        continue
    print(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
        flush=True,
    )
"""


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _server_entry(
    script: Path,
    *,
    approved: bool = True,
    tools: list[str] | None = None,
    request_timeout_seconds: float = 3,
) -> dict[str, Any]:
    return {
        "id": "mock",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "approved": approved,
        "tools": tools or ["echo"],
        "env": {"MCP_TOKEN": "HOST_MCP_TOKEN"},
        "startup_timeout_seconds": 3,
        "request_timeout_seconds": request_timeout_seconds,
    }


class McpConfigTests(unittest.TestCase):
    def test_loads_exact_command_allowlist_and_environment_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "server.py"
            script.write_text(_MOCK_SERVER, encoding="utf-8")
            path = root / "mcp.json"
            _write_json(path, {"servers": [_server_entry(script)]})

            config = load_mcp_config(path)
            server = config.servers[0]
            resolved = server.resolve_environment(
                {"HOST_MCP_TOKEN": "secret-value"}
            )

        self.assertEqual(server.allowed_tools, ("echo",))
        self.assertIn(str(script), server.display_command)
        self.assertEqual(resolved, {"MCP_TOKEN": "secret-value"})
        self.assertNotIn("secret-value", repr(server))

    def test_rejects_unapproved_command_unknown_fields_and_literal_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "server.py"
            script.write_text(_MOCK_SERVER, encoding="utf-8")
            path = root / "mcp.json"

            _write_json(
                path,
                {"servers": [_server_entry(script, approved=False)]},
            )
            with self.assertRaisesRegex(
                McpConfigurationError,
                "requires explicit approval",
            ):
                load_mcp_config(path)

            entry = _server_entry(script)
            entry["unexpected"] = True
            _write_json(path, {"servers": [entry]})
            with self.assertRaisesRegex(McpConfigurationError, "unknown fields"):
                load_mcp_config(path)

            entry = _server_entry(script)
            entry["env"] = {"MCP_TOKEN": "literal-secret-value"}
            _write_json(path, {"servers": [entry]})
            with self.assertRaisesRegex(
                McpConfigurationError,
                "must name a host environment variable",
            ):
                load_mcp_config(path)

    def test_missing_environment_error_names_variable_without_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "server.py"
            script.write_text(_MOCK_SERVER, encoding="utf-8")
            path = root / "mcp.json"
            _write_json(path, {"servers": [_server_entry(script)]})
            server = load_mcp_config(path).servers[0]

            with self.assertRaisesRegex(
                McpConfigurationError,
                "HOST_MCP_TOKEN",
            ):
                server.resolve_environment({})

    def test_tool_allowlist_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "server.py"
            script.write_text(_MOCK_SERVER, encoding="utf-8")
            path = root / "mcp.json"
            _write_json(
                path,
                {
                    "servers": [
                        _server_entry(
                            script,
                            tools=[f"tool-{index}" for index in range(65)],
                        )
                    ]
                },
            )

            with self.assertRaisesRegex(
                McpConfigurationError,
                "more than 64",
            ):
                load_mcp_config(path)


class _TimeoutClient:
    descriptors = (
        McpToolDescriptor(
            server_id="slow",
            name="mutate",
            description="A call with an unknown remote outcome.",
            input_schema={"type": "object", "properties": {}},
        ),
    )

    async def call_tool(self, **kwargs: Any) -> Any:
        raise McpCallError(
            McpCallErrorCode.TIMEOUT,
            server_id="slow",
            tool_name="mutate",
            cause_type="TimeoutError",
        )


class _UnsupportedSchemaClient:
    descriptors = (
        McpToolDescriptor(
            server_id="schema",
            name="unsupported",
            description="Uses a schema the Harness cannot enforce.",
            input_schema={"oneOf": [{"type": "object"}]},
        ),
    )

    async def call_tool(self, **kwargs: Any) -> Any:
        raise AssertionError("unsupported tool must never execute")


class McpToolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_manual_review_with_attempt_evidence(self) -> None:
        registry = ToolRegistry()
        tool = create_mcp_tools(_TimeoutClient())[0]
        registry.register(tool)

        pending = await registry.execute(tool.name, {})
        result = await registry.execute(
            tool.name,
            {},
            allow_confirm=True,
            idempotency_key="run:1:step:1",
        )

        self.assertEqual(pending.status, ToolResultStatus.REQUIRES_CONFIRMATION)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, ToolErrorType.TIMEOUT)
        self.assertFalse(result.recoverable_by_model)
        self.assertEqual(result.evidence[0].ref, "mcp://slow/mutate")
        self.assertEqual(result.side_effects[0].risk, "high")

    def test_unsupported_remote_schema_fails_closed_at_registration(self) -> None:
        registry = ToolRegistry()
        tool = create_mcp_tools(_UnsupportedSchemaClient())[0]

        with self.assertRaisesRegex(ValueError, "unsupported schema keywords"):
            registry.register(tool)

    def test_local_name_is_provider_safe_and_bounded(self) -> None:
        name = local_mcp_tool_name("server.one", "tool/" + "x" * 100)

        self.assertLessEqual(len(name), 64)
        self.assertRegex(name, r"^[A-Za-z0-9_-]+$")


class McpRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_tool_runs_only_after_approval_through_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "server.py"
            script.write_text(textwrap.dedent(_MOCK_SERVER), encoding="utf-8")
            config_path = root / "mcp.json"
            _write_json(config_path, {"servers": [_server_entry(script)]})
            services = build_application_runtime(
                config=ApplicationBootstrapConfig(
                    register_file_tools=False,
                    mcp_config_path=config_path,
                ),
                environ={"HOST_MCP_TOKEN": "secret-value"},
            )
            mcp_client = services.mcp_client
            self.assertIsNotNone(mcp_client)
            self.assertTrue(mcp_client.is_running)
            try:
                tool_name = "mcp__mock__echo"
                inventory = services.facade.inventory().to_dict()
                contract = TaskContract(
                    id="contract-mcp",
                    user_goal="call approved local MCP tool",
                    acceptance_criteria=(
                        AcceptanceCriterion(
                            id="mcp-evidence",
                            description="MCP call evidence exists",
                            type=CriterionType.TOOL_EVIDENCE,
                            evidence_refs=("mcp://mock/echo",),
                        ),
                    ),
                )
                first = await services.runtime.run(
                    contract=contract,
                    run_id="run-mcp",
                    steps=(
                        RuntimeToolStep(
                            id="call-echo",
                            tool_name=tool_name,
                            arguments={"text": "hello"},
                            required_evidence_refs=("mcp://mock/echo",),
                        ),
                    ),
                )
                approved = await services.runtime.resume_with_user_approval(
                    run_id="run-mcp",
                    feedback="approved local MCP command and tool call",
                )
                side_effects = services.long_task_store.list_side_effects(
                    "run-mcp"
                )
            finally:
                services.close()

        self.assertEqual(
            [item["name"] for item in inventory["tools"]],
            [tool_name],
        )
        self.assertEqual(first.run.status, TaskRunStatus.WAITING_USER)
        self.assertTrue(approved.accepted)
        self.assertEqual(approved.run.status, TaskRunStatus.COMPLETED)
        self.assertEqual(approved.tool_results[-1].content, 'hello|env:True\n{"echo": "hello"}')
        self.assertEqual(approved.tool_results[-1].evidence[0].ref, "mcp://mock/echo")
        self.assertEqual(side_effects[0].status, SideEffectStatus.CONFIRMED)
        self.assertFalse(side_effects[0].metadata["side_effect_retry_safe"])
        self.assertNotIn("secret-value", repr(approved.tool_results[-1].raw))
        self.assertFalse(mcp_client.is_running)

    async def test_real_stdio_request_timeout_is_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "server.py"
            script.write_text(textwrap.dedent(_MOCK_SERVER), encoding="utf-8")
            config_path = root / "mcp.json"
            _write_json(
                config_path,
                {
                    "servers": [
                        _server_entry(
                            script,
                            tools=["slow"],
                            request_timeout_seconds=1.5,
                        )
                    ]
                },
            )
            services = build_application_runtime(
                config=ApplicationBootstrapConfig(
                    register_file_tools=False,
                    mcp_config_path=config_path,
                ),
                environ={"HOST_MCP_TOKEN": "secret-value"},
            )
            try:
                result = await services.tools.execute(
                    "mcp__mock__slow",
                    {},
                    allow_confirm=True,
                )
            finally:
                services.close()

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, ToolErrorType.TIMEOUT)
        self.assertEqual(result.evidence[0].ref, "mcp://mock/slow")
        self.assertEqual(
            result.recommended_next_action.value,
            "manual_review",
        )

    def test_missing_allowlisted_tool_closes_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "server.py"
            script.write_text(textwrap.dedent(_MOCK_SERVER), encoding="utf-8")
            config_path = root / "mcp.json"
            _write_json(
                config_path,
                {"servers": [_server_entry(script, tools=["missing"])]},
            )

            with self.assertRaisesRegex(
                McpConfigurationError,
                "did not expose allowlisted tools: missing",
            ):
                build_application_runtime(
                    config=ApplicationBootstrapConfig(
                        register_file_tools=False,
                        mcp_config_path=config_path,
                    ),
                    environ={"HOST_MCP_TOKEN": "secret-value"},
                )


if __name__ == "__main__":
    unittest.main()
