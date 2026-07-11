"""open_gui: operate an Android phone through OpenGUI."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from loguru import logger

from ...agent.context import current_turn_context
from ...db.gui_devices import GuiDeviceBindingSnapshot, GuiDeviceBindingStore
from ...opengui import OpenGUIClient, OpenGUIClientError
from ..base import Tool, ToolPermission, ToolResult


_READ_ONLY_ACTIONS = frozenset({"devices", "apps", "current_binding", "status"})
_LOW_RISK_AUTO_ACTIONS = frozenset({
    "open_app",
    "do",
    "tap",
    "press_back",
    "press_home",
})
_DEFAULT_SCREEN_OBSERVATION = "观察当前手机屏幕并总结屏幕上显示的内容"
_SCREEN_OBSERVE_ALIASES = frozenset({"get", "observe", "inspect", "read", "screen"})
_HIGH_RISK_KEYWORDS = (
    "支付",
    "付款",
    "转账",
    "红包",
    "下单",
    "购买",
    "买单",
    "发送",
    "发消息",
    "删除",
    "清空",
    "授权",
    "登录",
    "验证码",
    "密码",
    "提现",
    "退款",
    "银行卡",
    "安全设置",
    "导出",
    "payment",
    "pay",
    "transfer",
    "purchase",
    "buy",
    "send",
    "delete",
    "authorize",
    "login",
    "password",
    "verification code",
    "otp",
    "export",
)

_SAFETY_SUFFIX = """\

执行安全边界：
- 只观察、打开页面、滚动、读取状态、总结内容时可以继续。
- 遇到支付、下单、发送消息、删除内容、授权登录、修改安全设置、输入密码、输入验证码、导出敏感数据时，必须暂停并请求用户确认，不要自行完成最终点击。
- 如果页面状态不确定，先暂停并说明你看到了什么。"""

_LOCAL_BACKEND_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "host.docker.internal",
})
_OPENGUI_ANDROID_PACKAGE = "com.coremate.opengui"
_BOOTSTRAP_DEVICE_POLL_ATTEMPTS = 15
_BOOTSTRAP_DEVICE_POLL_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True)
class _AdbBootstrapResult:
    attempted: bool
    message: str


class OpenGUITool(Tool):
    name = "open_gui"
    description = (
        "Control a bound Android phone through the OpenGUI backend. Use this"
        " when the user asks to open a mobile app, inspect the current phone"
        " screen, operate Android UI, check notifications inside an app, or"
        " continue/pause/cancel an OpenGUI execution.\n\n"
        "Actions: devices, apps, bind, current_binding, open_app, tap, do, status,"
        " pause, resume, cancel. Read-only actions"
        " (devices/apps/current_binding/status) execute without confirmation."
        " Low-risk phone actions (open_app, tap/click, observe/read-only do)"
        " also execute without confirmation unless the request contains"
        " payment, send, delete, authorization, password, or verification-code"
        " intent."
        " Use action=open_app when the user only wants to launch a phone app,"
        " action=tap with x/y only for ordinary coordinate clicks,"
        " and action=do for multi-step screen/app inspection. Use action=status"
        " for execution progress; do not invent action=get."
        " For tasks that may pay, send, delete, authorize,"
        " change security settings, or enter passwords/verification codes,"
        " instruct OpenGUI to pause and ask the user before the final action."
    )
    permission = ToolPermission.CONFIRM
    is_read_only = False
    is_concurrency_safe = False
    is_destructive = True
    max_result_chars = 12_000
    should_defer = True
    search_hint = (
        "open gui opengui android phone mobile app screen accessibility "
        "手机 安卓 打开app 看屏幕 操作手机 绑定手机执行器"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "devices",
                    "apps",
                    "bind",
                    "current_binding",
                    "open_app",
                    "tap",
                    "click",
                    "do",
                    "status",
                    "pause",
                    "resume",
                    "cancel",
                ],
                "description": "OpenGUI operation to perform.",
            },
            "device_id": {
                "type": "string",
                "description": (
                    "OpenGUI standby device id. Required for bind when more"
                    " than one device is online. Optional for do/open_app/apps"
                    " if the user already has a current binding."
                ),
            },
            "device_name": {
                "type": "string",
                "description": "Optional display name to store for bind.",
            },
            "description": {
                "type": "string",
                "description": (
                    "Natural-language phone task for action=do, e.g."
                    " '观察当前手机屏幕并总结'."
                ),
            },
            "task": {
                "type": "string",
                "description": (
                    "Alias for description when action=do. Prefer description,"
                    " but this is accepted for model compatibility."
                ),
            },
            "task_name": {
                "type": "string",
                "description": "Optional short task name for OpenGUI action=do.",
            },
            "app_name": {
                "type": "string",
                "description": (
                    "Human-readable app name for action=apps query or"
                    " action=open_app, e.g. 网易云音乐."
                ),
            },
            "package_name": {
                "type": "string",
                "description": (
                    "Android package name for action=open_app, e.g."
                    " com.netease.cloudmusic. Prefer package_name when known."
                ),
            },
            "query": {
                "type": "string",
                "description": "Optional app search query for action=apps.",
            },
            "x": {
                "type": "number",
                "description": "Screen X coordinate for action=tap/click.",
            },
            "y": {
                "type": "number",
                "description": "Screen Y coordinate for action=tap/click.",
            },
            "execution_id": {
                "type": "integer",
                "minimum": 1,
                "description": "OpenGUI execution id for status/pause/resume/cancel.",
            },
            "feedback": {
                "type": "string",
                "description": "Optional user feedback for action=resume.",
            },
            "base_url": {
                "type": "string",
                "description": (
                    "Optional OpenGUI backend URL override. Defaults to the"
                    " configured ZLAGENT_OPENGUI_BASE_URL or current binding."
                ),
            },
            "platform": {
                "type": "string",
                "description": (
                    "Optional IM platform override for offline tests. In normal"
                    " chat turns this comes from the current turn context."
                ),
            },
            "user_id": {
                "type": "string",
                "description": (
                    "Optional IM user id override for offline tests. In normal"
                    " chat turns this comes from the current turn context."
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 15.0,
        binding_store: Optional[GuiDeviceBindingStore] = None,
    ) -> None:
        self._base_url = (base_url or "http://localhost:7777").rstrip("/")
        self._timeout = max(1.0, float(timeout_seconds or 15.0))
        self._bindings = binding_store or GuiDeviceBindingStore()

    def is_action_read_only(self, arguments: dict[str, Any] | None) -> bool:
        if not isinstance(arguments, dict):
            return False
        action = _normalise_action(arguments)
        if action in _READ_ONLY_ACTIONS:
            return True
        if action == "open_app":
            return True
        return action in _LOW_RISK_AUTO_ACTIONS and not _contains_high_risk_intent(arguments)

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        raw_action = str(arguments.get("action") or "").strip().lower()
        action = _normalise_action(arguments)
        if not action:
            return ToolResult(ok=False, content="", error="action is required")
        if (
            action == "do"
            and raw_action in _SCREEN_OBSERVE_ALIASES
            and not _optional_string(arguments.get("description"))
        ):
            arguments = {**arguments, "description": _DEFAULT_SCREEN_OBSERVATION}
        try:
            if action == "devices":
                return await self._devices(arguments)
            if action == "apps":
                return await self._apps(arguments)
            if action == "bind":
                return await self._bind(arguments)
            if action == "current_binding":
                return self._current_binding(arguments)
            if action == "open_app":
                return await self._open_app(arguments)
            if action == "tap":
                return await self._tap(arguments)
            if action in {"press_back", "press_home"}:
                return await self._simple_phone_action(arguments, action=action)
            if action == "do":
                return await self._do(arguments)
            if action == "status":
                return await self._status(arguments)
            if action == "pause":
                return await self._execution_action(arguments, action="pause")
            if action == "resume":
                return await self._execution_action(arguments, action="resume")
            if action == "cancel":
                return await self._execution_action(arguments, action="cancel")
        except OpenGUIClientError as exc:
            return ToolResult(ok=False, content="", error=str(exc))
        except ValueError as exc:
            return ToolResult(ok=False, content="", error=str(exc))
        return ToolResult(ok=False, content="", error=f"unsupported action: {action}")

    async def _devices(self, args: dict[str, Any]) -> ToolResult:
        base_url = self._resolve_base_url(args)
        client = self._client(args, base_url=base_url)
        data, bootstrap = await self._list_devices_with_bootstrap(client, base_url=base_url)
        devices = _normalise_devices(data)
        if not devices:
            bootstrap_hint = f"\n\nLocal bootstrap: {bootstrap.message}" if bootstrap else ""
            return ToolResult(
                ok=True,
                content=(
                    "No OpenGUI devices are online. Start the OpenGUI Android"
                    " app, make sure the phone can reach the backend, and then"
                    f" run open_gui(action='devices') again.{bootstrap_hint}"
                ),
                raw=data,
            )
        lines = [f"OpenGUI online devices ({len(devices)}):"]
        for idx, device in enumerate(devices, start=1):
            name = device.get("deviceName") or device.get("device_id") or device.get("deviceId")
            device_id = device.get("deviceId") or device.get("device_id")
            app_count = device.get("appCount")
            suffix = f", apps={app_count}" if app_count is not None else ""
            lines.append(f"{idx}. {name} ({device_id}{suffix})")
        return ToolResult(ok=True, content="\n".join(lines), raw=data)

    async def _apps(self, args: dict[str, Any]) -> ToolResult:
        binding = self._optional_binding(args)
        base_url = self._resolve_base_url(args, binding=binding)
        client = self._client(args, base_url=base_url)
        device_id = await self._resolve_device_id(args, client=client, binding=binding)
        query = _optional_string(args.get("query")) or _optional_string(args.get("app_name"))
        payload = await self._list_device_apps_with_bootstrap(
            client,
            base_url=base_url,
            device_id=device_id,
            query=query,
        )
        apps = _normalise_apps(payload)
        if not apps:
            query_part = f" matching '{query}'" if query else ""
            return ToolResult(
                ok=True,
                content=(
                    f"No launchable apps{query_part} were reported by device {device_id}."
                    " Reopen the updated OpenGUI Android app if this list looks stale."
                ),
                raw=payload,
            )
        lines = [f"OpenGUI apps on {device_id} ({len(apps)} shown):"]
        for idx, app in enumerate(apps[:80], start=1):
            label = app.get("appName") or app.get("name") or app.get("packageName")
            package_name = app.get("packageName") or app.get("package_name")
            lines.append(f"{idx}. {label} ({package_name})")
        if len(apps) > 80:
            lines.append(f"... {len(apps) - 80} more")
        return ToolResult(ok=True, content="\n".join(lines), raw=payload)

    async def _bind(self, args: dict[str, Any]) -> ToolResult:
        platform, user_id = self._identity(args)
        base_url = self._resolve_base_url(args)
        client = self._client(args, base_url=base_url)
        data, _ = await self._list_devices_with_bootstrap(client, base_url=base_url)
        devices = _normalise_devices(data)
        if not devices:
            return ToolResult(ok=False, content="", error=_no_online_device_error(base_url))

        requested = str(args.get("device_id") or "").strip()
        selected: Optional[dict[str, Any]] = None
        if requested:
            selected = _find_device(devices, requested)
            if selected is None:
                return ToolResult(ok=False, content="", error=f"device '{requested}' is not online")
        elif len(devices) == 1:
            selected = devices[0]
        else:
            choices = ", ".join(
                f"{d.get('deviceName') or d.get('deviceId')} ({d.get('deviceId')})"
                for d in devices
            )
            return ToolResult(
                ok=False,
                content="",
                error=f"multiple OpenGUI devices are online; pass device_id. Choices: {choices}",
            )

        device_id = str(selected.get("deviceId") or selected.get("device_id") or "").strip()
        if not device_id:
            return ToolResult(ok=False, content="", error="selected device has no deviceId")
        device_name = str(
            args.get("device_name") or selected.get("deviceName") or device_id
        ).strip()
        snapshot = self._bindings.bind(
            platform=platform,
            user_id=user_id,
            opengui_base_url=base_url,
            device_id=device_id,
            device_name=device_name,
            verified_at=datetime.utcnow(),
        )
        logger.info(
            "bound OpenGUI device platform={} user={} device={} base_url={}",
            platform, user_id, device_id, base_url,
        )
        return ToolResult(
            ok=True,
            content=(
                "OpenGUI device bound.\n"
                f"- user: {platform}:{user_id}\n"
                f"- device: {snapshot.device_name} ({snapshot.device_id})\n"
                f"- backend: {snapshot.opengui_base_url}"
            ),
            raw={"binding": _binding_dict(snapshot)},
        )

    def _current_binding(self, args: dict[str, Any]) -> ToolResult:
        platform, user_id = self._identity(args)
        binding = self._bindings.get_current(platform=platform, user_id=user_id)
        if binding is None:
            return ToolResult(
                ok=True,
                content=(
                    f"No OpenGUI device is bound for {platform}:{user_id}."
                    " Run open_gui(action='devices') and then open_gui(action='bind')."
                ),
            )
        return ToolResult(
            ok=True,
            content=(
                "Current OpenGUI binding:\n"
                f"- user: {platform}:{user_id}\n"
                f"- device: {binding.device_name} ({binding.device_id})\n"
                f"- backend: {binding.opengui_base_url}\n"
                f"- last_verified_at: {binding.last_verified_at}"
            ),
            raw={"binding": _binding_dict(binding)},
        )

    async def _do(self, args: dict[str, Any]) -> ToolResult:
        description = _description_arg(args)
        if not description:
            return ToolResult(ok=False, content="", error="description is required for action=do")

        binding = self._optional_binding(args)
        base_url = self._resolve_base_url(args, binding=binding)
        device_id = str(args.get("device_id") or "").strip()
        if not device_id and binding is not None:
            device_id = binding.device_id
        if not device_id:
            return ToolResult(
                ok=False,
                content="",
                error=(
                    "no OpenGUI device bound for this user; run devices then bind,"
                    " or pass device_id explicitly"
                ),
            )

        client = self._client(args, base_url=base_url)
        payload = await self._dispatch_do_task_with_bootstrap(
            client,
            base_url=base_url,
            description=_with_safety_suffix(description),
            device_id=device_id,
            task_name=_optional_string(args.get("task_name")),
        )
        return ToolResult(ok=True, content=_render_json("OpenGUI task dispatched", payload), raw=payload)

    async def _open_app(self, args: dict[str, Any]) -> ToolResult:
        app_name = _optional_string(args.get("app_name"))
        package_name = _optional_string(args.get("package_name"))
        if not app_name and not package_name:
            return ToolResult(
                ok=False,
                content="",
                error="app_name or package_name is required for action=open_app",
            )

        binding = self._optional_binding(args)
        base_url = self._resolve_base_url(args, binding=binding)
        client = self._client(args, base_url=base_url)
        device_id = await self._resolve_device_id(args, client=client, binding=binding)

        if app_name and not package_name:
            apps_payload = await self._list_device_apps_with_bootstrap(
                client,
                base_url=base_url,
                device_id=device_id,
                query=app_name,
            )
            apps = _normalise_apps(apps_payload)
            matched_app, ambiguity = _select_app(apps, app_name)
            if ambiguity:
                choices = "\n".join(
                    f"- {app.get('appName') or app.get('name')} ({app.get('packageName') or app.get('package_name')})"
                    for app in ambiguity[:10]
                )
                return ToolResult(
                    ok=False,
                    content="",
                    error=(
                        f"multiple apps match '{app_name}'. Ask the user to choose one:\n{choices}"
                    ),
                    raw=apps_payload,
                )
            if matched_app is not None:
                package_name = _optional_string(
                    matched_app.get("packageName") or matched_app.get("package_name")
                )
                app_name = _optional_string(
                    matched_app.get("appName") or matched_app.get("name")
                ) or app_name

        description = _open_app_description(
            app_name=app_name,
            package_name=package_name,
        )
        payload = await self._dispatch_do_task_with_bootstrap(
            client,
            base_url=base_url,
            description=_with_safety_suffix(description),
            device_id=device_id,
            task_name=f"Open app: {app_name or package_name}",
        )
        return ToolResult(
            ok=True,
            content=_render_json("OpenGUI open_app dispatched", payload),
            raw={
                **payload,
                "resolved_app": {
                    "app_name": app_name,
                    "package_name": package_name,
                    "device_id": device_id,
                },
            },
        )

    async def _tap(self, args: dict[str, Any]) -> ToolResult:
        x = _coordinate_arg(args, "x")
        y = _coordinate_arg(args, "y")

        binding = self._optional_binding(args)
        base_url = self._resolve_base_url(args, binding=binding)
        client = self._client(args, base_url=base_url)
        device_id = await self._resolve_device_id(args, client=client, binding=binding)

        payload = await self._dispatch_do_task_with_bootstrap(
            client,
            base_url=base_url,
            description=_with_safety_suffix(_tap_description(x=x, y=y)),
            device_id=device_id,
            task_name=f"Tap: {x},{y}",
        )
        return ToolResult(
            ok=True,
            content=_render_json("OpenGUI tap dispatched", payload),
            raw={
                **payload,
                "resolved_action": {
                    "action": "tap",
                    "x": x,
                    "y": y,
                    "device_id": device_id,
                },
            },
        )

    async def _simple_phone_action(
        self,
        args: dict[str, Any],
        *,
        action: str,
    ) -> ToolResult:
        binding = self._optional_binding(args)
        base_url = self._resolve_base_url(args, binding=binding)
        client = self._client(args, base_url=base_url)
        device_id = await self._resolve_device_id(args, client=client, binding=binding)
        description = _simple_action_description(action)
        payload = await self._dispatch_do_task_with_bootstrap(
            client,
            base_url=base_url,
            description=_with_safety_suffix(description),
            device_id=device_id,
            task_name=action,
        )
        return ToolResult(
            ok=True,
            content=_render_json(f"OpenGUI {action} dispatched", payload),
            raw={
                **payload,
                "resolved_action": {
                    "action": action,
                    "device_id": device_id,
                },
            },
        )

    async def _status(self, args: dict[str, Any]) -> ToolResult:
        execution_id = _positive_int(args.get("execution_id"), "execution_id")
        client = self._client(args, base_url=self._resolve_base_url(args, binding=self._optional_binding(args)))
        payload = await client.get_execution(execution_id)
        return ToolResult(ok=True, content=_render_json("OpenGUI execution status", payload), raw=payload)

    async def _execution_action(self, args: dict[str, Any], *, action: str) -> ToolResult:
        execution_id = _positive_int(args.get("execution_id"), "execution_id")
        client = self._client(args, base_url=self._resolve_base_url(args, binding=self._optional_binding(args)))
        if action == "pause":
            payload = await client.pause_execution(execution_id)
        elif action == "resume":
            payload = await client.resume_execution(
                execution_id,
                feedback=_optional_string(args.get("feedback")),
            )
        elif action == "cancel":
            payload = await client.cancel_execution(execution_id)
        else:  # pragma: no cover - guarded by caller
            raise ValueError(f"unsupported execution action: {action}")
        return ToolResult(ok=True, content=_render_json(f"OpenGUI execution {action}", payload), raw=payload)

    def _client(
        self,
        args: dict[str, Any],
        *,
        base_url: Optional[str] = None,
    ) -> OpenGUIClient:
        return OpenGUIClient(
            base_url=base_url or self._resolve_base_url(args),
            timeout_seconds=self._timeout,
        )

    async def _resolve_device_id(
        self,
        args: dict[str, Any],
        *,
        client: OpenGUIClient,
        binding: Optional[GuiDeviceBindingSnapshot] = None,
    ) -> str:
        requested = str(args.get("device_id") or "").strip()
        if requested:
            return requested
        if binding is not None and binding.device_id:
            return binding.device_id
        data, _ = await self._list_devices_with_bootstrap(client, base_url=self._client_base_url(client))
        devices = _normalise_devices(data)
        if len(devices) == 1:
            device_id = str(devices[0].get("deviceId") or devices[0].get("device_id") or "").strip()
            if device_id:
                return device_id
        if not devices:
            raise ValueError(_no_online_device_error(self._client_base_url(client)))
        choices = ", ".join(
            f"{d.get('deviceName') or d.get('deviceId')} ({d.get('deviceId')})"
            for d in devices
        )
        raise ValueError(f"multiple OpenGUI devices are online; pass device_id. Choices: {choices}")

    def _resolve_base_url(
        self,
        args: dict[str, Any],
        *,
        binding: Optional[GuiDeviceBindingSnapshot] = None,
    ) -> str:
        explicit = str(args.get("base_url") or "").strip()
        if explicit:
            return explicit.rstrip("/")
        if binding is not None and binding.opengui_base_url:
            return binding.opengui_base_url.rstrip("/")
        return self._base_url

    async def _list_devices_with_bootstrap(
        self,
        client: OpenGUIClient,
        *,
        base_url: str,
    ) -> tuple[dict[str, Any], Optional[_AdbBootstrapResult]]:
        data = await client.list_devices()
        if _normalise_devices(data):
            return data, None

        bootstrap = await _bootstrap_local_android(base_url)
        if not bootstrap.attempted:
            return data, bootstrap

        for _ in range(_BOOTSTRAP_DEVICE_POLL_ATTEMPTS):
            await asyncio.sleep(_BOOTSTRAP_DEVICE_POLL_INTERVAL_SECONDS)
            data = await client.list_devices()
            if _normalise_devices(data):
                return data, bootstrap
        return data, bootstrap

    async def _list_device_apps_with_bootstrap(
        self,
        client: OpenGUIClient,
        *,
        base_url: str,
        device_id: str,
        query: Optional[str],
    ) -> dict[str, Any]:
        try:
            return await client.list_device_apps(device_id, query=query)
        except OpenGUIClientError as exc:
            if not _looks_like_offline_device_error(str(exc)):
                raise
            await self._list_devices_with_bootstrap(client, base_url=base_url)
            return await client.list_device_apps(device_id, query=query)

    async def _dispatch_do_task_with_bootstrap(
        self,
        client: OpenGUIClient,
        *,
        base_url: str,
        description: str,
        device_id: Optional[str],
        task_name: Optional[str],
    ) -> dict[str, Any]:
        try:
            return await client.do_task(
                description=description,
                device_id=device_id,
                task_name=task_name,
            )
        except OpenGUIClientError as exc:
            if not _looks_like_offline_device_error(str(exc)):
                raise
            await self._list_devices_with_bootstrap(client, base_url=base_url)
            return await client.do_task(
                description=description,
                device_id=device_id,
                task_name=task_name,
            )

    @staticmethod
    def _client_base_url(client: OpenGUIClient) -> str:
        return str(getattr(client, "base_url", "") or "").rstrip("/")

    def _optional_binding(self, args: dict[str, Any]) -> Optional[GuiDeviceBindingSnapshot]:
        try:
            platform, user_id = self._identity(args)
        except ValueError:
            return None
        return self._bindings.get_current(platform=platform, user_id=user_id)

    @staticmethod
    def _identity(args: dict[str, Any]) -> tuple[str, str]:
        platform = str(args.get("platform") or "").strip()
        user_id = str(args.get("user_id") or "").strip()
        if platform and user_id:
            return platform, user_id
        ctx = current_turn_context()
        if ctx is not None:
            platform = platform or (ctx.platform or "").strip()
            user_id = user_id or (ctx.user_id or "").strip()
        if not platform or not user_id:
            raise ValueError(
                "platform/user_id are required outside an active IM turn"
            )
        return platform, user_id


def _normalise_action(args: dict[str, Any]) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action == "get":
        if args.get("execution_id") and not _optional_string(args.get("description")):
            return "status"
        return "do"
    if action in {"observe", "inspect", "read", "screen"}:
        return "do"
    if action in {"list_devices", "device", "devices_list"}:
        return "devices"
    if action in {"list_apps", "apps", "app", "applications"}:
        return "apps"
    if action in {"launch_app", "start_app"}:
        return "open_app"
    if action in {"tap", "click"}:
        return "tap"
    if action in {"back", "press_back"}:
        return "press_back"
    if action in {"home", "press_home"}:
        return "press_home"
    if action in {"binding", "current", "current_device"}:
        return "current_binding"
    return action


async def _bootstrap_local_android(base_url: str) -> _AdbBootstrapResult:
    if not _should_bootstrap_local_android(base_url):
        return _AdbBootstrapResult(
            attempted=False,
            message="Skipped because the OpenGUI backend URL is not local.",
        )

    adb = shutil.which("adb")
    if not adb:
        return _AdbBootstrapResult(
            attempted=False,
            message="Skipped because adb is not available on PATH.",
        )

    port = _backend_port(base_url)
    if not port:
        return _AdbBootstrapResult(
            attempted=False,
            message=f"Skipped because no backend port could be parsed from {base_url}.",
        )

    devices_result = await _run_process([adb, "devices"])
    if devices_result[0] != 0:
        return _AdbBootstrapResult(
            attempted=True,
            message=f"adb devices failed: {devices_result[2] or devices_result[1]}",
        )

    serials = _parse_adb_device_serials(devices_result[1])
    if not serials:
        return _AdbBootstrapResult(
            attempted=True,
            message="adb is available, but no authorized Android device is connected.",
        )

    details: list[str] = []
    for serial in serials:
        reverse = await _run_process([
            adb,
            "-s",
            serial,
            "reverse",
            f"tcp:{port}",
            f"tcp:{port}",
        ])
        if reverse[0] == 0:
            details.append(f"{serial}: adb reverse tcp:{port} OK")
        else:
            details.append(
                f"{serial}: adb reverse failed: {reverse[2] or reverse[1]}"
            )
            continue

        launch = await _run_process([
            adb,
            "-s",
            serial,
            "shell",
            "monkey",
            "-p",
            _OPENGUI_ANDROID_PACKAGE,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ])
        if launch[0] == 0:
            details.append(f"{serial}: OpenGUI launch requested")
        else:
            details.append(
                f"{serial}: OpenGUI launch failed: {launch[2] or launch[1]}"
            )

    return _AdbBootstrapResult(
        attempted=True,
        message="; ".join(details),
    )


def _should_bootstrap_local_android(base_url: str) -> bool:
    flag = os.environ.get("ZLAGENT_OPENGUI_ADB_BOOTSTRAP", "1").strip().casefold()
    if flag in {"0", "false", "no", "off"}:
        return False
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").strip().casefold()
    return host in _LOCAL_BACKEND_HOSTS


def _backend_port(base_url: str) -> Optional[int]:
    parsed = urlparse(base_url)
    if parsed.port:
        return parsed.port
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    return None


async def _run_process(args: list[str], timeout_seconds: float = 8.0) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return 124, "", f"timed out after {timeout_seconds:.0f}s"
    return (
        process.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


def _parse_adb_device_serials(output: str) -> list[str]:
    serials: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serial = parts[0].strip()
            if serial and serial not in serials:
                serials.append(serial)
    return serials


def _no_online_device_error(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").strip().casefold()
    if host in _LOCAL_BACKEND_HOSTS:
        port = _backend_port(base_url) or 7777
        return (
            "no OpenGUI device is online; checked local adb bootstrap. "
            f"Make sure the Android app is installed/running and adb reverse tcp:{port} tcp:{port} is active."
        )
    return (
        "no OpenGUI device is online; make sure the Android app can reach "
        f"the configured backend {base_url}."
    )


def _looks_like_offline_device_error(message: str) -> bool:
    text = message.casefold()
    return (
        "device" in text
        and "online" in text
        and ("not online" in text or "no online" in text)
    )


def _normalise_devices(data: dict[str, Any]) -> list[dict[str, Any]]:
    devices = data.get("devices")
    if isinstance(devices, list):
        return [d for d in devices if isinstance(d, dict)]
    return []


def _normalise_apps(data: dict[str, Any]) -> list[dict[str, Any]]:
    apps = data.get("apps")
    if isinstance(apps, list):
        return [app for app in apps if isinstance(app, dict)]
    return []


def _find_device(devices: list[dict[str, Any]], requested: str) -> Optional[dict[str, Any]]:
    wanted = requested.strip()
    wanted_folded = wanted.casefold()
    for device in devices:
        candidates = (
            device.get("deviceId"),
            device.get("device_id"),
            device.get("deviceName"),
            device.get("device_name"),
            device.get("name"),
        )
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text and (text == wanted or text.casefold() == wanted_folded):
                return device
    return None


def _select_app(
    apps: list[dict[str, Any]],
    requested: str,
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    wanted = _normalise_app_text(requested)
    if not wanted:
        return None, []

    exact = [
        app for app in apps
        if _normalise_app_text(app.get("appName") or app.get("name")) == wanted
        or _normalise_app_text(app.get("packageName") or app.get("package_name")) == wanted
    ]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact

    fuzzy = [
        app for app in apps
        if wanted in _normalise_app_text(app.get("appName") or app.get("name"))
        or wanted in _normalise_app_text(app.get("packageName") or app.get("package_name"))
    ]
    if len(fuzzy) == 1:
        return fuzzy[0], []
    if len(fuzzy) > 1:
        return None, fuzzy
    return None, []


def _normalise_app_text(value: Any) -> str:
    text = str(value or "").strip().casefold()
    for suffix in ("app", "应用", "软件"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return "".join(text.split())


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _coordinate_arg(args: dict[str, Any], name: str) -> int:
    value = args.get(name)
    if value is None:
        value = args.get(f"start_{name}")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a screen coordinate") from None
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative screen coordinate")
    return int(round(parsed))


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _description_arg(args: dict[str, Any]) -> str:
    for key in ("description", "task", "instruction", "prompt", "query"):
        text = _optional_string(args.get(key))
        if text:
            return text
    return ""


def _with_safety_suffix(description: str) -> str:
    if "执行安全边界" in description:
        return description
    return description.rstrip() + _SAFETY_SUFFIX


def _open_app_description(
    *,
    app_name: Optional[str],
    package_name: Optional[str],
) -> str:
    label = app_name or package_name or "目标应用"
    parts = [
        f"打开手机 App：{label}。",
        "这是一个启动应用任务：优先使用 Android package/intent 直接打开，不要在桌面滑动寻找图标。",
    ]
    if package_name:
        parts.append(f"package_name='{package_name}'。")
    if app_name:
        parts.append(f"app_name='{app_name}'。")
    parts.append("打开成功后停留在该 App 当前页面，观察屏幕并简短汇报。")
    return "\n".join(parts)


def _tap_description(*, x: int, y: int) -> str:
    return "\n".join([
        f"点击当前手机屏幕坐标：x={x}, y={y}。",
        "这是普通导航点击任务，优先直接执行坐标点击，不要重新规划整套任务。",
        f"Action: click(point='<point>{x} {y}</point>')",
        "如果该坐标对应支付、发送、删除、授权、密码或验证码等高风险操作，必须暂停并请求用户确认。",
    ])


def _simple_action_description(action: str) -> str:
    if action == "press_back":
        return "\n".join([
            "执行普通导航动作：返回上一页。",
            "Action: press_back()",
            "如果返回会导致支付、发送、删除、授权或重要数据丢失，必须暂停并请求用户确认。",
        ])
    if action == "press_home":
        return "\n".join([
            "执行普通导航动作：回到手机桌面。",
            "Action: press_home()",
            "如果当前页面存在支付、发送、删除、授权或重要未保存内容，必须暂停并请求用户确认。",
        ])
    return f"执行普通手机动作：{action}。"


def _contains_high_risk_intent(value: Any) -> bool:
    text = _flatten_text(value).casefold()
    return any(keyword.casefold() in text for keyword in _HIGH_RISK_KEYWORDS)


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_flatten_text(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(v) for v in value)
    return str(value)


def _binding_dict(binding: GuiDeviceBindingSnapshot) -> dict[str, Any]:
    return {
        "id": binding.id,
        "platform": binding.platform,
        "user_id": binding.user_id,
        "opengui_base_url": binding.opengui_base_url,
        "device_id": binding.device_id,
        "device_name": binding.device_name,
        "enabled": binding.enabled,
        "last_verified_at": binding.last_verified_at.isoformat() if binding.last_verified_at else None,
    }


def _render_json(title: str, payload: dict[str, Any]) -> str:
    return f"{title}:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
