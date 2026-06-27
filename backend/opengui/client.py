"""Async REST client for the OpenGUI backend."""
from __future__ import annotations

from typing import Any, Optional

import httpx


class OpenGUIClientError(RuntimeError):
    """Raised when the OpenGUI backend is unreachable or rejects a request."""


class OpenGUIClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 15.0) -> None:
        self.base_url = (base_url or "http://localhost:7777").rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds or 15.0))

    async def list_devices(self) -> dict[str, Any]:
        return await self._request("GET", "/api/remote-control/devices")

    async def list_device_apps(
        self,
        device_id: str,
        *,
        query: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/remote-control/devices/{device_id}/apps",
            params=_compact({"q": query}),
        )

    async def do_task(
        self,
        *,
        description: str,
        device_id: Optional[str] = None,
        task_name: Optional[str] = None,
    ) -> dict[str, Any]:
        body = _compact({
            "description": description,
            "deviceId": device_id,
            "taskName": task_name,
        })
        return await self._request("POST", "/api/remote-control/tasks/do", json=body)

    async def get_execution(self, execution_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/api/remote-control/executions/{execution_id}")

    async def pause_execution(self, execution_id: int) -> dict[str, Any]:
        return await self._request("PUT", f"/api/remote-control/executions/{execution_id}/pause")

    async def resume_execution(
        self,
        execution_id: int,
        *,
        feedback: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/api/remote-control/executions/{execution_id}/resume",
            json=_compact({"feedback": feedback}),
        )

    async def cancel_execution(self, execution_id: int) -> dict[str, Any]:
        return await self._request("PUT", f"/api/remote-control/executions/{execution_id}/cancel")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=True) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise OpenGUIClientError(
                f"OpenGUI backend unreachable at {self.base_url}: {exc}"
            ) from exc

        data: Any
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not response.is_success:
            message = _extract_error(data) or f"HTTP {response.status_code}"
            raise OpenGUIClientError(f"OpenGUI request failed: {message}")
        if not isinstance(data, dict):
            raise OpenGUIClientError("OpenGUI returned a non-object JSON response")
        return data


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if v is not None}


def _extract_error(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "; ".join(str(item) for item in message)
    error = data.get("error")
    return error if isinstance(error, str) else ""
