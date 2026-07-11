"""Explicit provider configuration without persisting secret values."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .openai_compatible import (
    ChatCompletionTransport,
    OpenAICompatibleModelClient,
)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleModelConfig:
    """Non-secret settings used to construct an OpenAI-compatible client."""

    base_url: str
    model: str
    api_key_env: str | None = "OPENAI_API_KEY"
    timeout_seconds: float = 60
    temperature: float | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("model base_url must be non-empty")
        if not self.model.strip():
            raise ValueError("model name must be non-empty")
        if self.api_key_env is not None and not self.api_key_env.strip():
            raise ValueError("api_key_env must be non-empty or None")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

    def build_client(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        transport: ChatCompletionTransport | None = None,
    ) -> OpenAICompatibleModelClient:
        """Resolve the configured environment variable and build a client."""

        values = os.environ if environ is None else environ
        api_key = ""
        if self.api_key_env is not None:
            api_key = str(values.get(self.api_key_env, "")).strip()
            if not api_key:
                raise ValueError(
                    f"model API key environment variable is not set: {self.api_key_env}"
                )
        return OpenAICompatibleModelClient(
            base_url=self.base_url,
            model=self.model,
            api_key=api_key,
            timeout_seconds=self.timeout_seconds,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            transport=transport,
        )
