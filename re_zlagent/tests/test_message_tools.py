from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.tools import ToolErrorType, ToolRegistry, ToolResultStatus  # noqa: E402
from re_zlagent.harness.tools.builtins import SendMessageTool, ToolOutgoingMessage  # noqa: E402


class RecordingGateway:
    def __init__(self) -> None:
        self.sent: list[ToolOutgoingMessage] = []
        self.idempotency_keys: list[str] = []

    async def send(
        self,
        message: ToolOutgoingMessage,
        *,
        idempotency_key: str,
    ) -> None:
        self.sent.append(message)
        self.idempotency_keys.append(idempotency_key)


class FailingGateway:
    async def send(
        self,
        message: ToolOutgoingMessage,
        *,
        idempotency_key: str,
    ) -> None:
        raise RuntimeError("offline")


class SendMessageToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_requires_confirmation_before_sending(self) -> None:
        gateway = RecordingGateway()
        registry = ToolRegistry()
        registry.register(SendMessageTool(gateway))

        blocked = await registry.execute(
            "send_message",
            {
                "platform": "test",
                "target_type": "user",
                "target_id": "u1",
                "text": "hello",
            },
        )
        allowed = await registry.execute(
            "send_message",
            {
                "platform": "test",
                "target_type": "user",
                "target_id": "u1",
                "text": "hello",
            },
            allow_confirm=True,
        )

        self.assertEqual(blocked.status, ToolResultStatus.REQUIRES_CONFIRMATION)
        self.assertEqual(len(gateway.sent), 1)
        self.assertEqual(len(gateway.idempotency_keys), 1)
        self.assertTrue(gateway.idempotency_keys[0].startswith("direct:send_message:"))
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.side_effects[0].type, "message")
        self.assertEqual(allowed.evidence[0].ref, "test:u1")

    async def test_prepared_call_passes_the_stable_side_effect_key(self) -> None:
        gateway = RecordingGateway()
        registry = ToolRegistry()
        registry.register(SendMessageTool(gateway))
        arguments = {
            "platform": "test",
            "target_type": "user",
            "target_id": "u1",
            "text": "hello",
        }

        prepared = registry.prepare(
            "send_message",
            arguments,
            allow_confirm=True,
            idempotency_key="run:1:step:send",
        )
        result = await registry.execute_prepared(prepared)

        self.assertTrue(prepared.ready)
        self.assertTrue(prepared.outbox_required)
        self.assertEqual(
            prepared.context.side_effect_keys,
            ("run:1:step:send:side_effect:0",),
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            gateway.idempotency_keys,
            ["run:1:step:send:side_effect:0"],
        )

    async def test_invalid_input_is_recoverable(self) -> None:
        tool = SendMessageTool(RecordingGateway())

        result = await tool.execute({
            "platform": "",
            "target_type": "user",
            "target_id": "u1",
            "text": "hello",
        })

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, ToolErrorType.INVALID_INPUT)
        self.assertTrue(result.recoverable_by_model)

    async def test_gateway_failure_is_recoverable_external_unavailable(self) -> None:
        tool = SendMessageTool(FailingGateway())

        result = await tool.execute({
            "platform": "test",
            "target_type": "user",
            "target_id": "u1",
            "text": "hello",
        })

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, ToolErrorType.EXTERNAL_UNAVAILABLE)
        self.assertTrue(result.recoverable_by_model)


if __name__ == "__main__":
    unittest.main()
