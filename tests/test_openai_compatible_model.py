from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from re_zlagent.harness.model import (  # noqa: E402
    ModelClientError,
    ModelMessage,
    OpenAICompatibleModelConfig,
    OpenAICompatibleModelClient,
)


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.response


class OpenAICompatibleModelClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_posts_chat_completion_payload(self) -> None:
        transport = FakeTransport(
            {
                "choices": [
                    {
                        "message": {"content": '{"ok": true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10},
            }
        )
        client = OpenAICompatibleModelClient(
            base_url="https://provider.example/v1/",
            model="planner-model",
            api_key="secret",
            temperature=0,
            max_tokens=512,
            transport=transport,
        )

        response = await client.complete(
            (
                ModelMessage(role="system", content="plan"),
                ModelMessage(role="user", content="goal"),
            )
        )

        self.assertEqual(response.content, '{"ok": true}')
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://provider.example/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(call["payload"]["model"], "planner-model")
        self.assertEqual(call["payload"]["temperature"], 0)
        self.assertEqual(call["payload"]["max_tokens"], 512)
        self.assertEqual(call["payload"]["messages"][1]["content"], "goal")
        self.assertEqual(response.raw["finish_reason"], "stop")
        self.assertEqual(response.raw["usage"], {"prompt_tokens": 10})

    async def test_api_key_is_optional_for_local_compatible_servers(self) -> None:
        transport = FakeTransport(
            {
                "choices": [{"message": {"content": "ok"}}],
            }
        )
        client = OpenAICompatibleModelClient(
            base_url="http://localhost:8000/v1",
            model="local",
            transport=transport,
        )

        await client.complete((ModelMessage(role="user", content="hi"),))

        self.assertNotIn("Authorization", transport.calls[0]["headers"])

    async def test_call_output_cap_cannot_exceed_client_ceiling(self) -> None:
        transport = FakeTransport(
            {"choices": [{"message": {"content": "ok"}}]},
        )
        client = OpenAICompatibleModelClient(
            base_url="https://provider.example/v1",
            model="bounded",
            max_tokens=512,
            transport=transport,
        )

        await client.complete_with_options(
            (ModelMessage(role="user", content="first"),),
            max_tokens=128,
            response_format="json_object",
        )
        await client.complete_with_options(
            (ModelMessage(role="user", content="second"),),
            max_tokens=1_024,
        )

        self.assertEqual(transport.calls[0]["payload"]["max_tokens"], 128)
        self.assertEqual(
            transport.calls[0]["payload"]["response_format"],
            {"type": "json_object"},
        )
        self.assertEqual(transport.calls[1]["payload"]["max_tokens"], 512)

    async def test_content_parts_are_joined_when_provider_returns_list(self) -> None:
        transport = FakeTransport(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "hello"},
                                {"type": "text", "text": " world"},
                            ]
                        }
                    }
                ],
            }
        )
        client = OpenAICompatibleModelClient(
            base_url="https://provider.example/v1",
            model="planner-model",
            transport=transport,
        )

        response = await client.complete((ModelMessage(role="user", content="hi"),))

        self.assertEqual(response.content, "hello world")

    async def test_invalid_provider_response_is_error_data(self) -> None:
        transport = FakeTransport({"choices": []})
        client = OpenAICompatibleModelClient(
            base_url="https://provider.example/v1",
            model="planner-model",
            transport=transport,
        )

        with self.assertRaises(ModelClientError):
            await client.complete((ModelMessage(role="user", content="hi"),))

    def test_client_configuration_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            OpenAICompatibleModelClient(base_url="", model="model")
        with self.assertRaises(ValueError):
            OpenAICompatibleModelClient(
                base_url="https://provider.example/v1", model=""
            )
        with self.assertRaises(ValueError):
            OpenAICompatibleModelClient(
                base_url="https://provider.example/v1",
                model="model",
                timeout_seconds=0,
            )

    def test_environment_config_resolves_secret_without_storing_it(self) -> None:
        config = OpenAICompatibleModelConfig(
            base_url="https://provider.example/v1",
            model="planner-model",
            api_key_env="TEST_PROVIDER_KEY",
        )

        client = config.build_client(environ={"TEST_PROVIDER_KEY": "secret"})

        self.assertEqual(client.api_key, "secret")
        self.assertNotIn("secret", repr(config))

    def test_environment_config_requires_named_secret_unless_disabled(self) -> None:
        protected = OpenAICompatibleModelConfig(
            base_url="https://provider.example/v1",
            model="planner-model",
            api_key_env="MISSING_KEY",
        )
        with self.assertRaisesRegex(ValueError, "MISSING_KEY"):
            protected.build_client(environ={})

        local = OpenAICompatibleModelConfig(
            base_url="http://localhost:8000/v1",
            model="local",
            api_key_env=None,
        ).build_client(environ={})
        self.assertEqual(local.api_key, "")


if __name__ == "__main__":
    unittest.main()
