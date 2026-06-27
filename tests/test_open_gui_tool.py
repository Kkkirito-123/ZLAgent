from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.gui_devices import GuiDeviceBindingStore
from backend.db.models import Base
from backend.opengui.client import OpenGUIClient
from backend.tools.registry import ToolRegistry
from backend.tools.builtins.open_gui import OpenGUITool


class FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload


class FakeAsyncClient:
    queue = []
    requests = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method, url, json=None, params=None):
        self.__class__.requests.append({
            "method": method,
            "url": url,
            "json": json,
            "params": params,
        })
        if not self.__class__.queue:
            raise AssertionError("FakeAsyncClient queue is empty")
        payload = self.__class__.queue.pop(0)
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


@contextmanager
def isolated_gui_store():
    with TemporaryDirectory() as tmpdir:
        engine = create_engine(f"sqlite:///{tmpdir}/test.db", future=True)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

        @contextmanager
        def session_scope():
            session = Session()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        with patch("backend.db.gui_devices.session_scope", session_scope):
            yield GuiDeviceBindingStore()


class OpenGUIClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []

    async def test_client_uses_remote_control_paths(self):
        FakeAsyncClient.queue = [
            {"devices": [{"deviceId": "phone-1", "deviceName": "Pixel"}], "total": 1},
            {
                "deviceId": "phone-1",
                "apps": [{"appName": "网易云音乐", "packageName": "com.netease.cloudmusic"}],
                "total": 1,
            },
            {"success": True, "executionId": 7},
            {"id": 7, "executionStatus": "RUNNING"},
        ]
        with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
            client = OpenGUIClient(base_url="http://opengui.local:7777", timeout_seconds=3)
            await client.list_devices()
            await client.list_device_apps("phone-1", query="网易云")
            await client.do_task(description="观察屏幕", device_id="phone-1", task_name="peek")
            await client.get_execution(7)

        self.assertEqual(
            [r["method"] for r in FakeAsyncClient.requests],
            ["GET", "GET", "POST", "GET"],
        )
        self.assertEqual(
            [r["url"] for r in FakeAsyncClient.requests],
            [
                "http://opengui.local:7777/api/remote-control/devices",
                "http://opengui.local:7777/api/remote-control/devices/phone-1/apps",
                "http://opengui.local:7777/api/remote-control/tasks/do",
                "http://opengui.local:7777/api/remote-control/executions/7",
            ],
        )
        self.assertEqual(FakeAsyncClient.requests[1]["params"], {"q": "网易云"})
        self.assertEqual(
            FakeAsyncClient.requests[2]["json"],
            {"description": "观察屏幕", "deviceId": "phone-1", "taskName": "peek"},
        )


class GuiDeviceBindingStoreTest(unittest.TestCase):
    def test_bind_replaces_current_device_for_user(self):
        with isolated_gui_store() as store:
            first = store.bind(
                platform="weixin",
                user_id="u1",
                opengui_base_url="http://host:7777",
                device_id="phone-a",
                device_name="Phone A",
                verified_at=datetime.utcnow(),
            )
            second = store.bind(
                platform="weixin",
                user_id="u1",
                opengui_base_url="http://host:7777",
                device_id="phone-b",
                device_name="Phone B",
                verified_at=datetime.utcnow(),
            )
            current = store.get_current(platform="weixin", user_id="u1")

        self.assertEqual(first.device_id, "phone-a")
        self.assertEqual(second.device_id, "phone-b")
        self.assertIsNotNone(current)
        self.assertEqual(current.device_id, "phone-b")


class OpenGUIToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeAsyncClient.queue = []
        FakeAsyncClient.requests = []

    def test_read_only_actions_bypass_confirmation(self):
        tool = OpenGUITool(base_url="http://host:7777")
        self.assertTrue(tool.is_action_read_only({"action": "devices"}))
        self.assertTrue(tool.is_action_read_only({"action": "apps"}))
        self.assertTrue(tool.is_action_read_only({"action": "current_binding"}))
        self.assertTrue(tool.is_action_read_only({"action": "status"}))
        self.assertTrue(tool.is_action_read_only({"action": "open_app", "app_name": "网易云音乐"}))
        self.assertTrue(tool.is_action_read_only({"action": "open_app", "app_name": "支付宝"}))
        self.assertTrue(tool.is_action_read_only({"action": "do", "description": "观察当前屏幕并总结"}))
        self.assertTrue(tool.is_action_read_only({"action": "tap", "x": 540, "y": 1800}))
        self.assertTrue(tool.is_action_read_only({"action": "click", "x": 540, "y": 1800}))
        self.assertFalse(tool.is_action_read_only({"action": "do", "description": "给张三发送消息"}))
        self.assertFalse(tool.is_action_read_only({"action": "tap", "x": 540, "y": 1800, "reason": "确认支付"}))
        self.assertFalse(tool.is_action_read_only({"action": "bind"}))
        self.assertFalse(tool.is_action_read_only({"action": "cancel"}))

    async def test_tool_devices_bind_do_status_flow(self):
        FakeAsyncClient.queue = [
            {"devices": [{"deviceId": "phone-1", "deviceName": "Pixel"}], "total": 1},
            {"devices": [{"deviceId": "phone-1", "deviceName": "Pixel"}], "total": 1},
            {"success": True, "executionId": 9, "taskId": 2},
            {"id": 9, "executionStatus": "COMPLETED"},
        ]
        with isolated_gui_store() as store:
            tool = OpenGUITool(
                base_url="http://host:7777",
                timeout_seconds=3,
                binding_store=store,
            )
            with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
                devices = await tool.execute({"action": "devices"})
                bound = await tool.execute({
                    "action": "bind",
                    "platform": "weixin",
                    "user_id": "u1",
                })
                dispatched = await tool.execute({
                    "action": "do",
                    "platform": "weixin",
                    "user_id": "u1",
                    "description": "观察当前屏幕并总结",
                })
                status = await tool.execute({"action": "status", "execution_id": 9})

        self.assertTrue(devices.ok)
        self.assertTrue(bound.ok)
        self.assertTrue(dispatched.ok)
        self.assertTrue(status.ok)
        self.assertIn("执行安全边界", FakeAsyncClient.requests[2]["json"]["description"])
        self.assertEqual(FakeAsyncClient.requests[2]["json"]["deviceId"], "phone-1")
        self.assertTrue(FakeAsyncClient.requests[3]["url"].endswith("/executions/9"))

    async def test_bind_accepts_device_name_as_device_id(self):
        FakeAsyncClient.queue = [
            {"devices": [{"deviceId": "phone-1", "deviceName": "Samsung SM-S9280"}], "total": 1},
        ]
        with isolated_gui_store() as store:
            tool = OpenGUITool(
                base_url="http://host:7777",
                timeout_seconds=3,
                binding_store=store,
            )
            with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
                bound = await tool.execute({
                    "action": "bind",
                    "platform": "weixin",
                    "user_id": "u1",
                    "device_id": "Samsung SM-S9280",
                })
            current = store.get_current(platform="weixin", user_id="u1")

        self.assertTrue(bound.ok, bound.error)
        self.assertIsNotNone(current)
        self.assertEqual(current.device_id, "phone-1")
        self.assertEqual(current.device_name, "Samsung SM-S9280")

    async def test_get_with_execution_id_is_treated_as_status(self):
        FakeAsyncClient.queue = [
            {"id": 9, "executionStatus": "FINISHED", "executionResult": "SUCCEED"},
        ]
        tool = OpenGUITool(base_url="http://host:7777", timeout_seconds=3)

        with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
            result = await tool.execute({"action": "get", "execution_id": 9})

        self.assertTrue(result.ok, result.error)
        self.assertEqual(FakeAsyncClient.requests[0]["method"], "GET")
        self.assertTrue(FakeAsyncClient.requests[0]["url"].endswith("/executions/9"))

    async def test_get_without_execution_id_observes_current_screen(self):
        FakeAsyncClient.queue = [
            {"success": True, "executionId": 10, "taskId": 3},
        ]
        with isolated_gui_store() as store:
            store.bind(
                platform="weixin",
                user_id="u1",
                opengui_base_url="http://host:7777",
                device_id="phone-1",
                device_name="Pixel",
                verified_at=datetime.utcnow(),
            )
            tool = OpenGUITool(
                base_url="http://host:7777",
                timeout_seconds=3,
                binding_store=store,
            )

            with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
                result = await tool.execute({
                    "action": "get",
                    "platform": "weixin",
                    "user_id": "u1",
                })

        self.assertTrue(result.ok, result.error)
        self.assertEqual(FakeAsyncClient.requests[0]["method"], "POST")
        self.assertTrue(FakeAsyncClient.requests[0]["url"].endswith("/tasks/do"))
        self.assertIn("观察当前手机屏幕", FakeAsyncClient.requests[0]["json"]["description"])

    async def test_do_accepts_task_alias_for_description(self):
        FakeAsyncClient.queue = [
            {"success": True, "executionId": 11, "taskId": 4},
        ]
        with isolated_gui_store() as store:
            store.bind(
                platform="weixin",
                user_id="u1",
                opengui_base_url="http://host:7777",
                device_id="phone-1",
                device_name="Pixel",
                verified_at=datetime.utcnow(),
            )
            tool = OpenGUITool(
                base_url="http://host:7777",
                timeout_seconds=3,
                binding_store=store,
            )

            with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
                result = await tool.execute({
                    "action": "do",
                    "platform": "weixin",
                    "user_id": "u1",
                    "task": "打开网易云音乐，搜索林俊杰，并播放一首歌",
                })

        self.assertTrue(result.ok, result.error)
        self.assertEqual(FakeAsyncClient.requests[0]["method"], "POST")
        self.assertIn("打开网易云音乐", FakeAsyncClient.requests[0]["json"]["description"])

    async def test_apps_lists_bound_device_apps(self):
        FakeAsyncClient.queue = [
            {
                "deviceId": "phone-1",
                "apps": [
                    {"appName": "网易云音乐", "packageName": "com.netease.cloudmusic"},
                    {"appName": "QQ音乐", "packageName": "com.tencent.qqmusic"},
                ],
                "total": 2,
            },
        ]
        with isolated_gui_store() as store:
            store.bind(
                platform="weixin",
                user_id="u1",
                opengui_base_url="http://host:7777",
                device_id="phone-1",
                device_name="Pixel",
                verified_at=datetime.utcnow(),
            )
            tool = OpenGUITool(
                base_url="http://host:7777",
                timeout_seconds=3,
                binding_store=store,
            )

            with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
                result = await tool.execute({
                    "action": "apps",
                    "platform": "weixin",
                    "user_id": "u1",
                    "query": "音乐",
                })

        self.assertTrue(result.ok, result.error)
        self.assertIn("网易云音乐", result.content)
        self.assertEqual(FakeAsyncClient.requests[0]["method"], "GET")
        self.assertTrue(FakeAsyncClient.requests[0]["url"].endswith("/devices/phone-1/apps"))
        self.assertEqual(FakeAsyncClient.requests[0]["params"], {"q": "音乐"})

    async def test_open_app_resolves_package_from_reported_apps(self):
        FakeAsyncClient.queue = [
            {
                "deviceId": "phone-1",
                "apps": [
                    {"appName": "网易云音乐", "packageName": "com.netease.cloudmusic"},
                ],
                "total": 1,
            },
            {"success": True, "executionId": 12, "taskId": 5},
        ]
        with isolated_gui_store() as store:
            store.bind(
                platform="weixin",
                user_id="u1",
                opengui_base_url="http://host:7777",
                device_id="phone-1",
                device_name="Pixel",
                verified_at=datetime.utcnow(),
            )
            tool = OpenGUITool(
                base_url="http://host:7777",
                timeout_seconds=3,
                binding_store=store,
            )

            with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
                result = await tool.execute({
                    "action": "open_app",
                    "platform": "weixin",
                    "user_id": "u1",
                    "app_name": "网易云音乐",
                })

        self.assertTrue(result.ok, result.error)
        self.assertEqual(FakeAsyncClient.requests[0]["params"], {"q": "网易云音乐"})
        self.assertEqual(FakeAsyncClient.requests[1]["method"], "POST")
        body = FakeAsyncClient.requests[1]["json"]
        self.assertEqual(body["deviceId"], "phone-1")
        self.assertIn("package_name='com.netease.cloudmusic'", body["description"])
        self.assertIn("不要在桌面滑动寻找图标", body["description"])

    async def test_tap_dispatches_coordinate_task(self):
        FakeAsyncClient.queue = [
            {"success": True, "executionId": 13, "taskId": 6},
        ]
        with isolated_gui_store() as store:
            store.bind(
                platform="weixin",
                user_id="u1",
                opengui_base_url="http://host:7777",
                device_id="phone-1",
                device_name="Pixel",
                verified_at=datetime.utcnow(),
            )
            tool = OpenGUITool(
                base_url="http://host:7777",
                timeout_seconds=3,
                binding_store=store,
            )

            with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
                result = await tool.execute({
                    "action": "tap",
                    "platform": "weixin",
                    "user_id": "u1",
                    "x": 540,
                    "y": 1800,
                })

        self.assertTrue(result.ok, result.error)
        self.assertEqual(FakeAsyncClient.requests[0]["method"], "POST")
        body = FakeAsyncClient.requests[0]["json"]
        self.assertEqual(body["deviceId"], "phone-1")
        self.assertIn("Action: click(point='<point>540 1800</point>')", body["description"])

    async def test_registry_allows_read_only_action_without_confirm(self):
        FakeAsyncClient.queue = [
            {"devices": [{"deviceId": "phone-1", "deviceName": "Pixel"}], "total": 1},
        ]
        registry = ToolRegistry()
        registry.register(OpenGUITool(base_url="http://host:7777", timeout_seconds=3))
        with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
            result = await registry.execute("open_gui", {"action": "devices"})
        blocked = await registry.execute("open_gui", {"action": "cancel", "execution_id": 1})

        self.assertTrue(result.ok)
        self.assertFalse(blocked.ok)
        self.assertIn("requires user confirmation", blocked.error or "")

    async def test_registry_allows_low_risk_open_gui_without_confirm(self):
        FakeAsyncClient.queue = [
            {
                "deviceId": "phone-1",
                "apps": [
                    {"appName": "网易云音乐", "packageName": "com.netease.cloudmusic"},
                ],
                "total": 1,
            },
            {"success": True, "executionId": 14, "taskId": 7},
        ]
        with isolated_gui_store() as store:
            store.bind(
                platform="weixin",
                user_id="u1",
                opengui_base_url="http://host:7777",
                device_id="phone-1",
                device_name="Pixel",
                verified_at=datetime.utcnow(),
            )
            registry = ToolRegistry()
            registry.register(OpenGUITool(
                base_url="http://host:7777",
                timeout_seconds=3,
                binding_store=store,
            ))

            with patch("backend.opengui.client.httpx.AsyncClient", FakeAsyncClient):
                result = await registry.execute("open_gui", {
                    "action": "open_app",
                    "platform": "weixin",
                    "user_id": "u1",
                    "app_name": "网易云音乐",
                })

        self.assertTrue(result.ok, result.error)
        self.assertEqual(FakeAsyncClient.requests[-1]["method"], "POST")


if __name__ == "__main__":
    unittest.main()
