"""Interactive QR login for the personal-WeChat (Weixin) gateway.

Run inside the container with a TTY attached:

    docker exec -it zlagent python -m backend.cli.weixin_login

The QR code is rendered to stdout. Scan it with your personal WeChat to
authorize a new iLink bot identity. On success, credentials are written to
``<workspace>/credentials/weixin/accounts/<account_id>.json``.

v1.2.3 — by default the CLI now also hits the running ZLAgent API to
hot-reload credentials, register a DeliveryTarget, and send a test
message. Pass ``--no-auto-register`` to fall back to the manual flow.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from ..core.config import get_settings
from ..gateways._vendor import weixin_ilink as proto


def _http_post_json(url: str, body: Optional[dict] = None, timeout: float = 10.0) -> tuple[int, dict]:
    """Tiny stdlib-only POST helper. Returns (status_code, parsed_json_or_empty)."""
    data = b"" if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        return exc.code, {"error": body_text or str(exc)}
    except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
        return 0, {"error": str(exc)}


def _auto_register(api_base: str, user_id: str, display_name: str) -> int:
    """End-to-end orchestration after a successful QR login.

    Returns 0 on full success, 1 on partial (credentials saved but
    something downstream failed — the operator can still finish by
    hand with the printed instructions).
    """
    base = api_base.rstrip("/")
    print()
    print("=" * 60)
    print("正在自动完成后续步骤（连接到运行中的 ZLAgent API）...")
    print(f"  API base: {base}")
    print()

    # Step 1: hot-reload gateway so it picks up the new credentials.
    print("[1/3] 热加载 weixin gateway 凭据...")
    code, payload = _http_post_json(f"{base}/api/gateways/weixin/reload")
    if code != 200:
        print(f"  ✗ FAILED (HTTP {code}): {payload}")
        print()
        print("  ZLAgent 主进程可能没在跑。先 `docker compose up -d` 或 `python -m backend.app`，")
        print("  然后重新执行本命令（凭据已保存，不必再次扫码）。")
        return 1
    print(f"  ✓ OK: {payload}")

    # Step 2: create a DeliveryTarget for this user_id.
    print(f"[2/3] 注册 DeliveryTarget (user_id={user_id})...")
    code, payload = _http_post_json(
        f"{base}/api/delivery-targets",
        {
            "platform": "weixin",
            "target_type": "user",
            "target_id": user_id,
            "display_name": display_name,
        },
    )
    if code not in (200, 201):
        print(f"  ✗ FAILED (HTTP {code}): {payload}")
        return 1
    target_id = payload.get("id")
    print(f"  ✓ OK: target id={target_id}")

    # Step 3: send a test message to the new target.
    print(f"[3/3] 发送测试消息到 target {target_id}...")
    code, payload = _http_post_json(
        f"{base}/api/delivery-targets/{target_id}/test",
        timeout=20.0,
    )
    if code != 200:
        print(f"  ✗ FAILED (HTTP {code}): {payload}")
        print()
        print("  凭据和 DeliveryTarget 已经保存。测试消息失败通常是 iLink 临时限流")
        print("  （日志里会看到 ret=-2 / rate limited）。等 1-3 分钟后直接在微信")
        print("  给 bot 发一句“你好”，或手动重试测试消息即可。")
        return 1
    print(f"  ✓ OK: {payload}")

    print()
    print("=" * 60)
    print("✓ 全部完成。请在微信里查收来自 iLink bot 的测试消息。")
    print("=" * 60)
    return 0


def _print_qr(scan_url: str) -> None:
    print()
    print("请使用手机微信扫描以下二维码：")
    if scan_url:
        print(f"  扫码 URL: {scan_url}")
    try:
        import qrcode  # type: ignore[import-not-found]

        qr = qrcode.QRCode(border=1)
        qr.add_data(scan_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as exc:  # noqa: BLE001
        print(f"（终端二维码渲染失败：{exc}；可手动打开上方 URL 扫码）")
    print()


async def _on_event(event: dict) -> None:
    kind = event.get("kind")
    if kind == "qr":
        _print_qr(str(event.get("scan_url") or ""))
    elif kind == "scanned":
        print("已扫码，请在微信中点击「确认」...")
    elif kind == "expired":
        print(f"二维码已过期，正在刷新...({event.get('refresh')}/3)")
    elif kind == "redirect":
        print(f"iLink 切换到 {event.get('host')}")
    elif kind == "confirmed":
        print(f"登录成功：account_id={event.get('account_id')}")
    elif kind == "timeout":
        reason = event.get("reason")
        if reason:
            print(f"登录超时：{reason}")
        else:
            print("登录超时。")


def _resolve_weixin_home(custom: Optional[str]) -> str:
    if custom:
        return str(Path(custom).expanduser().resolve())
    settings = get_settings()
    settings.ensure_directories()
    return str(settings.workspace_dir / "credentials")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Authorize a personal-WeChat iLink bot identity for ZLAgent."
    )
    parser.add_argument(
        "--bot-type",
        default="3",
        help="iLink bot_type parameter (default: 3, matches Hermes upstream).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=480,
        help="Total seconds to wait for QR scan + confirm (default: 480).",
    )
    parser.add_argument(
        "--weixin-home",
        default=None,
        help=(
            "Override credential storage root. Defaults to "
            "<workspace>/credentials inside the container."
        ),
    )
    parser.add_argument(
        "--api-base",
        default="http://localhost:8020",
        help=(
            "ZLAgent API base URL for auto-register (default: "
            "http://localhost:8020). Set via ZLAGENT_PORT if your bot "
            "uses a non-default port."
        ),
    )
    parser.add_argument(
        "--display-name",
        default="我的微信",
        help="DeliveryTarget display name (default: 我的微信).",
    )
    parser.add_argument(
        "--no-auto-register",
        action="store_true",
        help=(
            "Skip the post-login auto-register (reload + create target "
            "+ test). Fall back to printing the manual cURL / "
            "Invoke-RestMethod instructions."
        ),
    )
    args = parser.parse_args(argv)

    if not proto.check_weixin_requirements():
        print(
            "ERROR: missing dependencies. Install aiohttp + cryptography (and "
            "optionally qrcode for terminal QR rendering).",
            file=sys.stderr,
        )
        return 2

    weixin_home = _resolve_weixin_home(args.weixin_home)
    print(f"credentials will be saved under: {weixin_home}")

    try:
        result: Optional[dict[str, Any]] = asyncio.run(
            proto.qr_login(
                weixin_home,
                bot_type=args.bot_type,
                timeout_seconds=args.timeout,
                on_event=_on_event,
            )
        )
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130

    if result is None:
        print("登录未完成。")
        return 1

    account_id = result.get("account_id", "")
    user_id = result.get("user_id", "")
    base_url = result.get("base_url", "")

    print()
    print("=" * 60)
    print("Weixin gateway 凭据已保存。")
    print(f"  account_id : {account_id}")
    print(f"  user_id    : {user_id}")
    print(f"  base_url   : {base_url}")
    print("=" * 60)

    if not args.no_auto_register and user_id:
        return _auto_register(args.api_base, user_id, args.display_name)

    # Manual fallback (--no-auto-register, or QR succeeded without user_id).
    print()
    print("下一步（手动）：")
    print("  1. 创建 DeliveryTarget（PowerShell）:")
    print("       $body = @{")
    print('         platform="weixin"; target_type="user";')
    print(f'         target_id="{user_id or "<user_id>"}"; display_name="我的微信"')
    print("       } | ConvertTo-Json")
    print("       Invoke-RestMethod -Method Post `")
    print("         -Uri http://localhost:8020/api/delivery-targets `")
    print("         -ContentType application/json -Body $body")
    print("  2. 热加载凭据：")
    print("       Invoke-RestMethod -Method Post -Uri http://localhost:8020/api/gateways/weixin/reload")
    print("  3. 发测试消息：")
    print("       Invoke-RestMethod -Method Post -Uri http://localhost:8020/api/delivery-targets/<id>/test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
