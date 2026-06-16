"""Personal WeChat (Weixin) gateway via Tencent's iLink Bot protocol.

This adapter bridges ZLAgent's :class:`MessageGateway` to the iLink protocol
implementation that lives under :mod:`backend.gateways._vendor.weixin_ilink`.
The vendored module is a focused port of Hermes Agent's iLink code (MIT) — see
that file's header for attribution.

What it does:
  * Loads saved credentials from ``<workspace>/weixin/accounts/<account_id>.json``
  * Sends text messages to a recipient's iLink user_id (typically the QR
    scanner — i.e. you).
  * Handles session expiry (errcode=-14) by transparently retrying once
    without ``context_token``.
  * Handles iLink rate limit (-2) with bounded backoff.
  * Polls inbound text DMs and forwards them into ZLAgent's gateway manager.

Setup workflow:
  1. ``docker exec -it zlagent python -m backend.cli.weixin_login``
  2. Scan the printed QR with your personal WeChat.
  3. The CLI prints your ``user_id``; paste it into a DeliveryTarget with
     ``platform=weixin``, ``target_type=user``.
  4. Restart the container so the gateway picks up the new credentials,
     or call ``POST /api/gateways/weixin/reload``.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from loguru import logger

from ..core.config import get_settings
from ._vendor import weixin_ilink as proto
from .base import DeliveryTarget, IncomingMessage, MessageGateway, MessageHandler, OutgoingMessage


def _weixin_home(workspace_dir: Path) -> str:
    return str(workspace_dir / "credentials")


class WeixinGateway(MessageGateway):
    """ZLAgent gateway for personal WeChat via the iLink Bot protocol."""

    platform = "weixin"
    kind = "ilink"

    # v0.22 status surface ---------------------------------------------------
    def is_configured(self) -> bool:
        return bool(getattr(self, "_configured", False))

    def status_extra(self) -> dict:
        return {
            "account_id": proto._safe_id(getattr(self, "_account_id", "") or ""),
            "base_url": getattr(self, "_base_url", ""),
            "polling": bool(
                getattr(self, "_poll_task", None) is not None
                and not getattr(self, "_poll_task").done()
            ),
        }

    def __init__(
        self,
        handler: MessageHandler,
        *,
        account_id: Optional[str] = None,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        weixin_home: Optional[str] = None,
        send_chunk_retries: int = 8,
        send_chunk_retry_delay_seconds: float = 2.0,
    ) -> None:
        super().__init__(handler)
        settings = get_settings()
        self._weixin_home = weixin_home or _weixin_home(settings.workspace_dir)
        self._account_id = account_id or os.getenv("WEIXIN_ACCOUNT_ID", "").strip()
        self._token = (token or os.getenv("WEIXIN_TOKEN", "")).strip()
        self._base_url = (base_url or os.getenv("WEIXIN_BASE_URL", proto.ILINK_BASE_URL)).strip().rstrip("/")
        self._send_chunk_retries = max(0, int(send_chunk_retries))
        self._send_chunk_retry_delay = max(0.0, float(send_chunk_retry_delay_seconds))
        self._token_store: Optional[proto.ContextTokenStore] = None
        self._session = None  # aiohttp.ClientSession lazily created on start()
        self._poll_task: Optional[asyncio.Task] = None
        self._sync_buf = ""
        self._lock = asyncio.Lock()
        self._configured = False
        # handler tasks fired-and-forgotten by the poll loop. We
        # track them so shutdown can drain in-flight handlers; we never
        # await them inline because doing so would serialise the poll
        # loop and turn IM into a one-message-at-a-time bot.
        self._handler_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if not proto.check_weixin_requirements():
            logger.warning(
                "weixin gateway disabled: aiohttp / cryptography are not installed"
            )
            return

        self._reload_credentials()

        if not self._configured:
            logger.info(
                "weixin gateway registered but not configured yet "
                "(run `docker exec -it zlagent python -m backend.cli.weixin_login`)"
            )
            return

        import aiohttp  # local import keeps the optional dep out of cold paths

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=True, connector=proto._make_ssl_connector()
            )
        self._load_sync_buf()
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_updates())
        logger.info(
            "weixin gateway ready (account_id={}, base_url={})",
            proto._safe_id(self._account_id),
            self._base_url,
        )

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        # drain background handler tasks. Bounded wait so a stuck
        # LLM call cannot keep the process alive past 10s during shutdown;
        # anything still in flight is cancelled.
        if self._handler_tasks:
            pending = tuple(self._handler_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                for task in pending:
                    if not task.done():
                        task.cancel()
                logger.warning(
                    "weixin stop: cancelled %d in-flight handler task(s) after 10s",
                    sum(1 for t in pending if not t.done()),
                )
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("weixin gateway: session close raised: {}", exc)
            self._session = None
        logger.info("weixin gateway stopped")

    # ------------------------------------------------------------------ outbound
    async def send(self, message: OutgoingMessage) -> None:
        if not self._configured:
            raise RuntimeError(
                "weixin gateway is not configured; run the QR login CLI first"
            )
        chat_id = message.target.target_id
        if not chat_id:
            raise ValueError("weixin send: target_id (recipient user_id) is required")

        # when a RichMessage is attached, render it through the
        # plain-text renderer (CJK width-aware column alignment, KV
        # padding, etc) and use *that* as the wire body. The iLink
        # transport itself can only send text items (no public image /
        # card item kind), so we lean on visual structure inside the
        # text envelope. ``message.text`` is the safety net: the agent
        # layer always populates it with the LLM's narrative wrapper
        # so an empty rich render still produces a legible message.
        body_text: str = message.text
        if message.rich is not None and not message.rich.is_empty():
            try:
                from ..rich.renderers import render_plain_text

                rendered = render_plain_text(message.rich).strip()
            except Exception as exc:  # noqa: BLE001 - never break the send path
                logger.warning(
                    "[weixin rich] render failed: {} — using OutgoingMessage.text",
                    exc,
                )
                rendered = ""
            if rendered:
                body_text = rendered

        if not body_text or not body_text.strip():
            raise ValueError("weixin send: text must not be empty")

        async with self._lock:
            session = await self._ensure_session()
            assert self._token_store is not None
            context_token = self._token_store.get(self._account_id, chat_id)
            retried_without_token = False

            for attempt in range(self._send_chunk_retries + 1):
                try:
                    resp = await proto.send_text_message(
                        session,
                        base_url=self._base_url,
                        token=self._token,
                        to=chat_id,
                        text=body_text,
                        context_token=context_token,
                    )
                except Exception as exc:  # noqa: BLE001 - network / aiohttp errors
                    if attempt >= self._send_chunk_retries:
                        raise
                    wait = self._send_chunk_retry_delay * (attempt + 1)
                    logger.warning(
                        "weixin send transport error to={} attempt={}/{}: {}",
                        proto._safe_id(chat_id),
                        attempt + 1,
                        self._send_chunk_retries + 1,
                        exc,
                    )
                    await asyncio.sleep(wait)
                    continue

                expired, rate_limited, errmsg = proto._check_session_error(resp)
                if expired or rate_limited or errmsg:
                    logger.warning(
                        "weixin send raw response to {}: {}",
                        proto._safe_id(chat_id),
                        resp,
                    )
                if expired and not retried_without_token and context_token:
                    retried_without_token = True
                    self._token_store.forget(self._account_id, chat_id)
                    context_token = None
                    logger.warning(
                        "weixin send: session expired for {}; retrying without context_token",
                        proto._safe_id(chat_id),
                    )
                    continue
                if rate_limited:
                    if attempt >= self._send_chunk_retries:
                        raise RuntimeError(
                            f"weixin send rate limited (errmsg={errmsg!r})"
                        )
                    wait = min(60.0, self._send_chunk_retry_delay * (2 ** attempt))
                    logger.warning(
                        "weixin send: rate limited for {}; backing off {:.1f}s (attempt {}/{})",
                        proto._safe_id(chat_id),
                        wait,
                        attempt + 1,
                        self._send_chunk_retries + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                if expired or errmsg:
                    raise RuntimeError(
                        f"weixin send error: ret={resp.get('ret')} errcode={resp.get('errcode')} errmsg={errmsg!r}"
                    )

                logger.info(
                    "[weixin send] target={} len={} rich={} ok",
                    message.target.describe(),
                    len(body_text),
                    "yes" if (message.rich is not None and not message.rich.is_empty()) else "no",
                )
                return

            raise RuntimeError("weixin send: exhausted retries without success")

    # ------------------------------------------------------------------ helpers
    async def _ensure_session(self):
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                trust_env=True, connector=proto._make_ssl_connector()
            )
        return self._session

    async def _poll_updates(self) -> None:
        while self._configured:
            try:
                session = await self._ensure_session()
                resp = await proto.get_updates(
                    session,
                    base_url=self._base_url,
                    token=self._token,
                    sync_buf=self._sync_buf,
                )
                next_buf = str(resp.get("get_updates_buf") or self._sync_buf or "")
                if next_buf != self._sync_buf:
                    self._sync_buf = next_buf
                    self._save_sync_buf()

                assert self._token_store is not None
                for msg in resp.get("msgs") or []:
                    sender_id = str(msg.get("from_user_id") or "").strip()
                    context_token = str(msg.get("context_token") or "").strip()
                    if sender_id and context_token:
                        self._token_store.set(self._account_id, sender_id, context_token)
                        logger.info(
                            "weixin poll: captured context_token for {}",
                            proto._safe_id(sender_id),
                        )
                    incoming = self._message_to_incoming(msg)
                    if incoming is not None:
                        # Fire-and-forget so the poll loop keeps draining
                        # the inbox while the agent is busy. Per-user
                        # concurrency is enforced inside the gateway
                        # manager (see GatewayManager docstring).
                        task = asyncio.create_task(self._safe_emit(incoming))
                        self._handler_tasks.add(task)
                        task.add_done_callback(self._handler_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("weixin poll error: {}", exc)
                await asyncio.sleep(5)

    async def _safe_emit(self, message: IncomingMessage) -> None:
        """Drive ``self.emit`` while swallowing exceptions.

        We are intentionally a sink-of-last-resort here: anything that
        propagates would silently kill the background task and we'd never
        know. Logging plus discard is the right behaviour. Per-user
        concurrency enforcement happens in the manager downstream of
        ``emit``.
        """
        try:
            await self.emit(message)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "weixin handler failed for user={}: {}",
                proto._safe_id(message.user_id or ""),
                exc,
            )

    def _message_to_incoming(self, msg: dict) -> Optional[IncomingMessage]:
        sender_id = str(msg.get("from_user_id") or "").strip()
        if not sender_id:
            return None
        if sender_id == self._account_id:
            return None
        if int(msg.get("message_type") or 0) == proto.MSG_TYPE_BOT:
            return None

        text_parts: list[str] = []
        for item in msg.get("item_list") or []:
            text = str((item.get("text_item") or {}).get("text") or "").strip()
            if text:
                text_parts.append(text)
        text = "\n".join(text_parts).strip()
        if not text:
            return None

        message_id = str(
            msg.get("msg_id")
            or msg.get("message_id")
            or msg.get("client_id")
            or f"weixin-{sender_id}-{hash(text)}"
        )
        return IncomingMessage(
            platform=self.platform,
            channel_id=sender_id,
            user_id=sender_id,
            message_id=message_id,
            text=text,
            reply_target=DeliveryTarget(
                platform=self.platform,
                target_type="user",
                target_id=sender_id,
                display_name=sender_id,
            ),
            raw=msg,
        )

    def _sync_buf_path(self) -> Path:
        return (
            Path(self._weixin_home)
            / "weixin"
            / "accounts"
            / f"{self._account_id}.get-updates.json"
        )

    def _load_sync_buf(self) -> None:
        path = self._sync_buf_path()
        if not path.exists():
            self._sync_buf = ""
            return
        try:
            import json

            self._sync_buf = str(json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf") or "")
        except Exception:
            self._sync_buf = ""

    def _save_sync_buf(self) -> None:
        try:
            import json

            path = self._sync_buf_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"get_updates_buf": self._sync_buf}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("weixin poll: failed to save sync buffer: {}", exc)

    def _reload_credentials(self) -> None:
        """Refresh state from disk + env. Call after a successful QR login."""
        if not self._account_id:
            self._account_id = os.getenv("WEIXIN_ACCOUNT_ID", "").strip()
        if not self._token:
            self._token = os.getenv("WEIXIN_TOKEN", "").strip()

        if self._account_id and not self._token:
            persisted = proto.load_weixin_account(self._weixin_home, self._account_id)
            if persisted:
                self._token = str(persisted.get("token") or "").strip()
                self._base_url = (
                    str(persisted.get("base_url") or self._base_url).strip().rstrip("/")
                )

        # No account_id yet: auto-pick a saved account if any are
        # available on disk. With a single saved account this is
        # unambiguous; with multiple we pick the newest mtime, which
        # matches the "I just re-scanned the QR" case 99% of the time.
        # Set WEIXIN_ACCOUNT_ID explicitly to override.
        if not self._account_id:
            saved = self._discover_saved_accounts()
            picked: Optional[str] = None
            if len(saved) == 1:
                picked = saved[0]
            elif len(saved) > 1:
                accounts_dir = Path(self._weixin_home) / "weixin" / "accounts"
                try:
                    picked = max(
                        saved,
                        key=lambda aid: (accounts_dir / f"{aid}.json").stat().st_mtime,
                    )
                    logger.warning(
                        "weixin gateway: {} saved accounts found, picking newest"
                        " ({}). Set WEIXIN_ACCOUNT_ID to pin a specific one.",
                        len(saved),
                        proto._safe_id(picked),
                    )
                except OSError as exc:
                    logger.warning(
                        "weixin gateway: stat() failed while picking newest"
                        " account: {}; staying unconfigured", exc,
                    )
            if picked:
                self._account_id = picked
                persisted = proto.load_weixin_account(self._weixin_home, self._account_id)
                if persisted:
                    self._token = str(persisted.get("token") or "").strip()
                    self._base_url = (
                        str(persisted.get("base_url") or self._base_url).strip().rstrip("/")
                    )
                    logger.info(
                        "weixin gateway: auto-selected saved account {}",
                        proto._safe_id(self._account_id),
                    )

        if self._account_id and self._token:
            self._token_store = proto.ContextTokenStore(self._weixin_home)
            self._token_store.restore(self._account_id)
            self._configured = True
        else:
            self._configured = False
            self._token_store = None

    def _discover_saved_accounts(self) -> list[str]:
        accounts_dir = Path(self._weixin_home) / "weixin" / "accounts"
        if not accounts_dir.exists():
            return []
        ids: list[str] = []
        for child in accounts_dir.iterdir():
            if not child.is_file() or child.suffix != ".json":
                continue
            if child.name.endswith((".context-tokens.json", ".get-updates.json")):
                continue
            try:
                persisted = proto.load_weixin_account(self._weixin_home, child.stem)
            except Exception:
                persisted = None
            if persisted and persisted.get("token"):
                ids.append(child.stem)
        return sorted(ids)

    # ------------------------------------------------------------------ introspection
    def status(self) -> dict:
        return {
            "configured": self._configured,
            "account_id": self._account_id or None,
            "base_url": self._base_url,
            "weixin_home": self._weixin_home,
            "saved_accounts": self._discover_saved_accounts(),
        }

    async def reload(self) -> dict:
        """Re-read credentials from disk (e.g. after running the QR login CLI).

        Truly re-discovers: if disk now has a newer saved account than
        the one currently in memory (common after re-scanning the QR),
        we switch to it. Without this the gateway would keep using the
        first account it picked at startup even after a fresh login.
        Existing in-flight sessions / poll tasks are torn down so the
        new credentials get a clean state.
        """
        # Decide whether the on-disk picture moved past what we hold.
        # We deliberately do NOT clear ``WEIXIN_ACCOUNT_ID``-pinned
        # state — env override remains authoritative.
        env_pinned = bool(os.getenv("WEIXIN_ACCOUNT_ID", "").strip())
        if not env_pinned:
            saved = self._discover_saved_accounts()
            accounts_dir = Path(self._weixin_home) / "weixin" / "accounts"
            newest: Optional[str] = None
            if len(saved) == 1:
                newest = saved[0]
            elif len(saved) > 1:
                try:
                    newest = max(
                        saved,
                        key=lambda aid: (accounts_dir / f"{aid}.json").stat().st_mtime,
                    )
                except OSError:
                    newest = None
            if newest and newest != self._account_id:
                logger.info(
                    "weixin reload: switching active account {} -> {}",
                    proto._safe_id(self._account_id or ""),
                    proto._safe_id(newest),
                )
                # Tear down the stale poll + http session so the new
                # account starts clean (otherwise we'd reuse cookies /
                # context_tokens tied to the previous identity).
                if self._poll_task is not None and not self._poll_task.done():
                    self._poll_task.cancel()
                self._poll_task = None
                if self._session is not None and not self._session.closed:
                    try:
                        await self._session.close()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("weixin reload: session close raised: {}", exc)
                self._session = None
                self._account_id = newest
                self._token = ""  # force _reload_credentials to re-read from disk
                self._configured = False

        self._reload_credentials()
        if self._configured:
            await self._ensure_session()
            self._load_sync_buf()
            if self._poll_task is None or self._poll_task.done():
                self._poll_task = asyncio.create_task(self._poll_updates())
        return self.status()
