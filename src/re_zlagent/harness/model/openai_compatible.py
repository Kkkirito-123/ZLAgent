"""OpenAI-compatible chat completion model client.

The client is intentionally small and dependency-free. Production code may
inject a richer transport, while tests can use a deterministic fake transport.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error, request

from .types import ModelClient, ModelMessage, ModelResponse


class ModelClientError(RuntimeError):
    """Raised when a model provider response cannot be used."""


class ChatCompletionTransport(Protocol):
    """Transport boundary for OpenAI-compatible chat completions."""

    async def complete(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Return a provider JSON response."""


class UrllibChatCompletionTransport:
    """Minimal stdlib HTTP transport for chat completions."""

    async def complete(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._post_json,
            url,
            headers,
            payload,
            timeout_seconds,
        )

    def _post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ModelClientError(f"model HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise ModelClientError(f"model transport error: {exc.reason}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelClientError(f"model response is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ModelClientError("model response root must be an object")
        return data


@dataclass(slots=True)
class OpenAICompatibleModelClient(ModelClient):
    """ModelClient implementation for OpenAI-compatible chat completions."""

    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 60
    temperature: float | None = None
    max_tokens: int | None = None
    transport: ChatCompletionTransport | None = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.model = self.model.strip()
        self.api_key = self.api_key.strip()
        if not self.base_url:
            raise ValueError("base_url must be non-empty")
        if not self.model:
            raise ValueError("model must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.transport is None:
            self.transport = UrllibChatCompletionTransport()

    async def complete(self, messages: tuple[ModelMessage, ...]) -> ModelResponse:
        return await self.complete_with_options(messages)

    async def complete_with_options(
        self,
        messages: tuple[ModelMessage, ...],
        *,
        max_tokens: int | None = None,
        response_format: str | None = None,
    ) -> ModelResponse:
        """Complete with an optional call-level output cap.

        A client-level maximum remains a hard ceiling when both limits exist.
        """

        if not messages:
            raise ValueError("messages must not be empty")
        if max_tokens is not None and max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if response_format not in {None, "json_object"}:
            raise ValueError("unsupported response_format")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        token_limits = [
            limit for limit in (self.max_tokens, max_tokens) if limit is not None
        ]
        if token_limits:
            payload["max_tokens"] = min(token_limits)
        if response_format is not None:
            payload["response_format"] = {"type": response_format}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        assert self.transport is not None
        raw = await self.transport.complete(
            url=f"{self.base_url}/chat/completions",
            headers=headers,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        content = _extract_content(raw)
        return ModelResponse(
            content=content,
            raw={
                "provider": "openai-compatible",
                "model": self.model,
                "finish_reason": _extract_finish_reason(raw),
                "usage": dict(raw.get("usage") or {}),
                "response": raw,
            },
        )


def _extract_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelClientError("model response missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ModelClientError("model choice must be an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ModelClientError("model choice missing message")
    content = message.get("content")
    if isinstance(content, str):
        if not content.strip():
            raise ModelClientError("model response content is empty")
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "".join(parts)
        if text.strip():
            return text
    raise ModelClientError("model response contained no text content")


def _extract_finish_reason(data: dict[str, Any]) -> str | None:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    reason = choices[0].get("finish_reason")
    return reason if isinstance(reason, str) else None
