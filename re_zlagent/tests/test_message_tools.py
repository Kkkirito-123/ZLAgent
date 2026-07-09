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

    async def send(self, message: ToolOutgoingMessage) -> None:
        self.sent.append(message)


class FailingGateway:
    async def send(self, message: ToolOutgoingMessage) -> None:
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
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.side_effects[0].type, "message")
        self.assertEqual(allowed.evidence[0].ref, "test:u1")

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
