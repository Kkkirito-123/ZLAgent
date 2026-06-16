"""Tencent iLink Bot protocol layer for personal WeChat (Weixin).

This module is a focused port of the iLink Bot protocol implementation from
Hermes Agent's ``gateway/platforms/weixin.py``. Only the pieces required to
authenticate (QR login), persist credentials, send text messages, and run the
long-poll loop are kept here. The Hermes-specific framework wrappers
(``BasePlatformAdapter``, ``MessageEvent``, dedup helpers, …) are intentionally
omitted so ZLAgent can compose the protocol against its own
``MessageGateway`` interface without inheriting Hermes's runtime model.

Original source:
    https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/weixin.py

Original license: MIT (see THIRD_PARTY_NOTICES.md). The MIT copyright notice
is preserved in this file header.

----------------------------------------------------------------------
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
----------------------------------------------------------------------
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency gate
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency gate
    default_backend = None  # type: ignore[assignment]
    Cipher = None  # type: ignore[assignment]
    algorithms = None  # type: ignore[assignment]
    modes = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants — verbatim from Hermes upstream so the protocol stays compatible.
# ---------------------------------------------------------------------------

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2

ITEM_TEXT = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2


# ---------------------------------------------------------------------------
# Public availability check.
# ---------------------------------------------------------------------------

def check_weixin_requirements() -> bool:
    """Return True when runtime dependencies are importable."""
    return AIOHTTP_AVAILABLE and CRYPTO_AVAILABLE


# ---------------------------------------------------------------------------
# Small helpers (logging-safe id, json dump, AES, headers).
# ---------------------------------------------------------------------------

def _safe_id(value: Optional[str], keep: int = 8) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "?"
    if len(raw) <= keep:
        return raw
    return raw[:keep]


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> Dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _is_stale_session_ret(
    ret: Optional[int], errcode: Optional[int], errmsg: Optional[str]
) -> bool:
    """True when iLink returns ret=-2 / errcode=-2 with 'unknown error'.

    Hermes upstream documented this as a stale-session signal rather than a
    genuine rate limit — preserved verbatim.
    """
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


def _make_ssl_connector():
    """Return a TCPConnector with a certifi CA bundle, or ``None`` if unavailable.

    Tencent's iLink server is not always verifiable against system CA stores;
    when ``certifi`` is installed we use its Mozilla bundle to guarantee
    verification. Otherwise fall back to aiohttp defaults.
    """
    if not AIOHTTP_AVAILABLE:
        return None
    try:
        import ssl

        import certifi
    except ImportError:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


# ---------------------------------------------------------------------------
# Account credential persistence (one JSON per account_id under a base dir).
# ---------------------------------------------------------------------------

def _account_dir(weixin_home: str) -> Path:
    path = Path(weixin_home) / "weixin" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _account_file(weixin_home: str, account_id: str) -> Path:
    return _account_dir(weixin_home) / f"{account_id}.json"


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically (tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_weixin_account(
    weixin_home: str,
    *,
    account_id: str,
    token: str,
    base_url: str,
    user_id: str = "",
) -> None:
    """Persist account credentials so the gateway can reuse them across boots."""
    payload = {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = _account_file(weixin_home, account_id)
    _atomic_json_write(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_weixin_account(weixin_home: str, account_id: str) -> Optional[Dict[str, Any]]:
    path = _account_file(weixin_home, account_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# context_token cache: iLink expects every outbound reply to echo back the
# latest context_token observed for that peer. We persist it to disk so cron
# pushes survive process restarts.
# ---------------------------------------------------------------------------

class ContextTokenStore:
    """Disk-backed ``context_token`` cache keyed by (account_id, peer)."""

    def __init__(self, weixin_home: str) -> None:
        self._root = _account_dir(weixin_home)
        self._cache: Dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self._root / f"{account_id}.context-tokens.json"

    @staticmethod
    def _key(account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "weixin: failed to restore context tokens for %s: %s",
                _safe_id(account_id),
                exc,
            )
            return
        for user_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, user_id)] = token

    def get(self, account_id: str, user_id: str) -> Optional[str]:
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        self._cache[self._key(account_id, user_id)] = token
        self._persist(account_id)

    def forget(self, account_id: str, user_id: str) -> None:
        self._cache.pop(self._key(account_id, user_id), None)
        self._persist(account_id)

    def _persist(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        payload = {
            key[len(prefix):]: value
            for key, value in self._cache.items()
            if key.startswith(prefix)
        }
        try:
            _atomic_json_write(self._path(account_id), payload)
        except Exception as exc:
            logger.warning(
                "weixin: failed to persist context tokens for %s: %s",
                _safe_id(account_id),
                exc,
            )


# ---------------------------------------------------------------------------
# HTTP API helpers (lifted verbatim; only the import path differs).
# ---------------------------------------------------------------------------

async def _api_post(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: Optional[str],
    timeout_ms: int,
) -> Dict[str, Any]:
    body = _json_dumps({**payload, "base_info": _base_info()})
    url = f"{base_url.rstrip('/')}/{endpoint}"
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(url, data=body, headers=_headers(token, body), timeout=timeout) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
        return json.loads(raw)


async def _api_get(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.get(url, headers=headers, timeout=timeout) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
        return json.loads(raw)


async def get_updates(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int = LONG_POLL_TIMEOUT_MS,
) -> Dict[str, Any]:
    try:
        return await _api_post(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def send_text_message(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: Optional[str] = None,
    client_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a plain text message via iLink ``sendmessage``.

    Returns the raw API response dict; callers should inspect ``ret`` and
    ``errcode`` for session expiry / rate limit signals.
    """
    if not text or not text.strip():
        raise ValueError("send_text_message: text must not be empty")
    cid = client_id or f"zlagent-weixin-{uuid.uuid4().hex}"
    message: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": cid,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": message},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


# ---------------------------------------------------------------------------
# QR login flow.
# ---------------------------------------------------------------------------

QrEvent = Dict[str, Any]
QrCallback = Callable[[QrEvent], Awaitable[None]]


async def qr_login(
    weixin_home: str,
    *,
    bot_type: str = "3",
    timeout_seconds: int = 480,
    on_event: Optional[QrCallback] = None,
) -> Optional[Dict[str, str]]:
    """Run the interactive iLink QR login flow.

    ``on_event`` receives a series of dicts so callers can render UI as they
    please (terminal, web page, …). Recognised events:

      * ``{"kind": "qr", "scan_url": "...", "qrcode": "<hex token>"}``
      * ``{"kind": "scanned"}``
      * ``{"kind": "expired", "refresh": int}``
      * ``{"kind": "redirect", "host": "..."}``
      * ``{"kind": "confirmed", "account_id": "...", "user_id": "..."}``
      * ``{"kind": "timeout"}``

    Returns the credential dict on success, or ``None`` on timeout/failure.
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for Weixin QR login")

    async def emit(event: QrEvent) -> None:
        if on_event is not None:
            try:
                await on_event(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning("weixin: on_event callback raised: %s", exc)

    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        try:
            qr_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.error("weixin: failed to fetch QR code: %s", exc)
            return None

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            logger.error("weixin: QR response missing qrcode token")
            return None

        scan_url = qrcode_url or qrcode_value
        await emit({"kind": "qr", "scan_url": scan_url, "qrcode": qrcode_value})

        deadline = time.time() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.time() < deadline:
            try:
                status_resp = await _api_get(
                    session,
                    base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                logger.warning("weixin: QR poll error: %s", exc)
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")
            if status == "wait":
                pass
            elif status == "scaned":
                await emit({"kind": "scanned"})
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
                    await emit({"kind": "redirect", "host": redirect_host})
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    await emit({"kind": "timeout", "reason": "qr_refresh_exhausted"})
                    return None
                await emit({"kind": "expired", "refresh": refresh_count})
                try:
                    qr_resp = await _api_get(
                        session,
                        base_url=ILINK_BASE_URL,
                        endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                        timeout_ms=QR_TIMEOUT_MS,
                    )
                    qrcode_value = str(qr_resp.get("qrcode") or "")
                    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                    scan_url = qrcode_url or qrcode_value
                    await emit({"kind": "qr", "scan_url": scan_url, "qrcode": qrcode_value})
                except Exception as exc:
                    logger.error("weixin: QR refresh failed: %s", exc)
                    return None
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    logger.error("weixin: QR confirmed but credential payload incomplete")
                    return None
                save_weixin_account(
                    weixin_home,
                    account_id=account_id,
                    token=token,
                    base_url=base_url,
                    user_id=user_id,
                )
                await emit({"kind": "confirmed", "account_id": account_id, "user_id": user_id})
                return {
                    "account_id": account_id,
                    "token": token,
                    "base_url": base_url,
                    "user_id": user_id,
                }
            await asyncio.sleep(1)

        await emit({"kind": "timeout"})
        return None


__all__ = [
    "AIOHTTP_AVAILABLE",
    "CRYPTO_AVAILABLE",
    "ILINK_BASE_URL",
    "WEIXIN_CDN_BASE_URL",
    "RATE_LIMIT_ERRCODE",
    "SESSION_EXPIRED_ERRCODE",
    "ContextTokenStore",
    "_is_stale_session_ret",
    "_make_ssl_connector",
    "_safe_id",
    "check_weixin_requirements",
    "get_updates",
    "load_weixin_account",
    "qr_login",
    "save_weixin_account",
    "send_text_message",
]


def _check_session_error(resp: Dict[str, Any]) -> Tuple[bool, bool, str]:
    """Inspect an iLink response and classify its terminal error state.

    Returns ``(is_session_expired, is_rate_limited, errmsg)``.
    """
    if not isinstance(resp, dict):
        return False, False, ""
    ret = resp.get("ret")
    errcode = resp.get("errcode")
    errmsg = str(resp.get("errmsg") or resp.get("msg") or "")
    if (ret in (None, 0)) and (errcode in (None, 0)):
        return False, False, errmsg
    is_session_expired = (
        ret == SESSION_EXPIRED_ERRCODE
        or errcode == SESSION_EXPIRED_ERRCODE
        or _is_stale_session_ret(ret, errcode, errmsg)
    )
    is_rate_limited = ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE
    if is_rate_limited and is_session_expired:
        # When _is_stale_session_ret triggers, prefer the session-expired branch
        # so callers retry without ``context_token`` instead of blindly waiting.
        is_rate_limited = False
    return is_session_expired, is_rate_limited, errmsg


__all__.append("_check_session_error")
