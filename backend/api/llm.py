"""HTTP control surface for the LLM client.

These endpoints are operational helpers — they let you verify LLM connectivity
without going through an IM channel and without pretending to be a chat
session. The agent loop and cron runner are the real consumers of the client.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..llm import LLMClient, LLMMessage

router = APIRouter(prefix="/api/llm", tags=["llm"])


def _get_client(request: Request) -> LLMClient:
    client = getattr(request.app.state, "llm", None)
    if client is None:
        raise HTTPException(status_code=500, detail="LLM client not initialized")
    return client


class LLMStatus(BaseModel):
    configured: bool
    provider: str
    model: str
    base_url: str


class LLMTestPayload(BaseModel):
    text: str = Field(..., description="User-side text to send to the LLM.")
    system: Optional[str] = Field(
        default=None,
        description=(
            "Optional system prompt override. Defaults to a short ZLAgent"
            " operator prompt suitable for connectivity tests."
        ),
    )


class LLMTestResponse(BaseModel):
    content: str
    model: str
    provider: str


@router.get("/status", response_model=LLMStatus)
async def status(request: Request) -> LLMStatus:
    client = _get_client(request)
    return LLMStatus(
        configured=client.configured,
        provider=client.provider,
        model=client.model,
        base_url=client.base_url,
    )


@router.post("/test", response_model=LLMTestResponse)
async def test_chat(payload: LLMTestPayload, request: Request) -> LLMTestResponse:
    client = _get_client(request)
    if not client.configured:
        raise HTTPException(
            status_code=409,
            detail=(
                "LLM is not configured; set OPENAI_API_KEY + OPENAI_MODEL"
                " (or OPENAI_BASE_URL for an OpenAI-compatible endpoint) and"
                " restart the container"
            ),
        )
    system = payload.system or (
        "You are ZLAgent's connectivity test probe."
        " Reply with one short Chinese sentence acknowledging the user."
    )
    try:
        response = await client.chat(
            [
                LLMMessage(role="system", content=system),
                LLMMessage(role="user", content=payload.text),
            ]
        )
    except Exception as exc:  # noqa: BLE001 - surface the upstream failure
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    return LLMTestResponse(
        content=response.content,
        model=response.model,
        provider=response.provider,
    )
