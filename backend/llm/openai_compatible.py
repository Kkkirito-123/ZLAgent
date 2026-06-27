"""OpenAI-compatible chat completions client (with tools / function calling).

Supports the OpenAI tools API shape (``tools=[...]`` parameter,
``tool_calls`` in the assistant message, ``role="tool"`` response messages).
The same wire format is accepted by OpenAI, DeepSeek, Moonshot, OpenRouter,
Qwen, Together and vLLM, so no provider branching is required.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional

import httpx
from loguru import logger

from .prompt_cache import strip_system_prompt_cache_boundary
from .sanitize import (
    StreamingHallucinationScrubber,
    contains_hallucinated_tool_call,
    strip_hallucinated_tool_calls,
)
from ..core.config import Settings

Role = Literal["system", "user", "assistant", "tool"]

# token-level streaming callback. The chat client invokes this
# coroutine for every ``content`` delta it sees on the SSE stream, but
# never for ``reasoning_content`` (DeepSeek-R1 thinking) or after the
# first ``tool_calls`` delta is observed (the response transitioned to
# tool-use mode and partial content stream is now ambiguous). Pass
# ``None`` (the default) to keep the v0.37.8 aggregate-then-return
# behaviour.
StreamCallback = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class LLMToolCall:
    """A single function tool call emitted by the model."""

    id: str
    name: str
    arguments: str  # JSON-encoded string per OpenAI spec; callers must json.loads


@dataclass(slots=True)
class LLMMessage:
    """Message for the chat completions protocol.

    ``tool_call_id`` / ``name`` are only meaningful for ``role="tool"`` (the
    result of a tool invocation fed back to the model). ``tool_calls`` is only
    meaningful for ``role="assistant"`` messages that requested one or more
    tool invocations.

    ``reasoning_content`` is a provider-specific extension used by DeepSeek
    reasoning models (R1, V4-Pro thinking mode, ...). Those endpoints embed
    the model's chain-of-thought there and *require* it to be echoed back in
    subsequent assistant turns; omitting it triggers HTTP 400. Other
    providers ignore the field, so it is safe to always pass through.
    """

    role: Role
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[LLMToolCall]] = None
    reasoning_content: Optional[str] = None
    # v0.40.8 — inbound image attachments forwarded to vision-capable
    # models. Only meaningful for ``role="user"``; other roles ignore
    # this field. Each entry is an ``http(s)://`` URL the provider can
    # fetch (the OpenAI multimodal protocol also accepts data URLs but
    # we don't construct those today — IM gateways already give us
    # CDN URLs). When non-empty, ``to_api()`` switches the message's
    # ``content`` from a plain string to a list of content parts:
    # ``[{type:text, ...}, {type:image_url, image_url:{url:...}}]``.
    # Models without vision return an HTTP 400 from the provider; this
    # is a deliberate "surface the error early" design rather than
    # silently dropping image_url parts.
    images: Optional[list[str]] = None

    def to_api(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content or ""}
        # v0.40.8 multimodal serialization. Only user messages may carry
        # image_url parts; tool results / assistant responses keep the
        # plain-string shape because the spec doesn't define multimodal
        # for those roles.
        if self.images and self.role == "user":
            parts: list[dict[str, Any]] = []
            text_part = self.content or ""
            if text_part:
                parts.append({"type": "text", "text": text_part})
            for url in self.images:
                if not url:
                    continue
                parts.append({"type": "image_url", "image_url": {"url": url}})
            if parts:
                out["content"] = parts
        if self.name:
            out["name"] = self.name
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.reasoning_content:
            out["reasoning_content"] = self.reasoning_content
        return out

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly serialization for persistence (v0.6 confirmation flow).

        Distinct from :meth:`to_api` because it always preserves every field
        (including ``role`` even when content is empty), so :meth:`from_dict`
        can perfectly round-trip the message back into the agent loop.
        """
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            out["name"] = self.name
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in self.tool_calls
            ]
        if self.reasoning_content is not None:
            out["reasoning_content"] = self.reasoning_content
        if self.images:
            out["images"] = list(self.images)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMMessage":
        tool_calls_raw = data.get("tool_calls") or None
        tool_calls: Optional[list[LLMToolCall]] = None
        if tool_calls_raw:
            tool_calls = [
                LLMToolCall(
                    id=str(tc.get("id") or ""),
                    name=str(tc.get("name") or ""),
                    arguments=str(tc.get("arguments") or ""),
                )
                for tc in tool_calls_raw
                if isinstance(tc, dict)
            ]
        images_raw = data.get("images") or None
        images: Optional[list[str]] = None
        if isinstance(images_raw, list):
            images = [str(u) for u in images_raw if isinstance(u, str) and u]
            if not images:
                images = None
        return cls(
            role=data.get("role", "user"),  # type: ignore[arg-type]
            content=data.get("content", "") or "",
            name=data.get("name"),
            tool_call_id=data.get("tool_call_id"),
            tool_calls=tool_calls,
            reasoning_content=data.get("reasoning_content"),
            images=images,
        )


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str
    provider: str = "openai-compatible"
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    reasoning_content: Optional[str] = None
    raw: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=dict)
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.provider = settings.llm_provider.strip().lower()
        self.base_url = settings.openai_base_url.rstrip("/")
        self.api_key = settings.openai_api_key.strip()
        self.model = (settings.openai_model or settings.default_model or "gpt-4o-mini").strip()
        self.timeout_seconds = settings.llm_timeout_seconds
        self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_tokens
        # Provider-side prompt caching + token streaming. Both default to on;
        # operators can disable via settings when a provider misbehaves.
        self.prompt_cache_enabled = bool(
            getattr(settings, "prompt_cache_enabled", True)
        )
        self.stream_enabled = bool(getattr(settings, "llm_stream_enabled", True))
        # v0.40.8 — vision auto-downgrade state.
        # Starts False: we optimistically send image_url content parts
        # whenever an inbound message carries attachments. The first time
        # the provider rejects with an HTTP 400 whose body mentions
        # ``image_url`` (or ``vision`` / ``multimodal``), we flip this
        # flag and rebuild the payload with images rewritten as text
        # placeholders (``[用户附带了图片：<url>]``). The flag is process-
        # local: a restart re-probes vision support, which is useful when
        # the operator switches to a vision-capable model without
        # touching the flag. Operators can pre-seed the downgrade by
        # setting ``settings.llm_vision_supported = False`` — useful for
        # deterministic local tests.
        vision_supported = getattr(settings, "llm_vision_supported", None)
        self._vision_unsupported: bool = (
            vision_supported is False  # only an explicit False flips it on
        )

    @property
    def configured(self) -> bool:
        if self.provider not in {"openai", "openai-compatible"}:
            return False
        return bool(self.model and (self.api_key or self.base_url != "https://api.openai.com/v1"))

    # ----- v0.40.8 vision downgrade plumbing ------------------------------

    @staticmethod
    def _looks_like_vision_rejection(err_text: str) -> bool:
        """Return True when an HTTP 400 body indicates the model rejected
        an ``image_url`` content part.

        DeepSeek returns ``unknown variant `image_url`, expected `text```
        — case-sensitive and extremely specific. Other providers phrase it
        as ``vision`` / ``multimodal`` not supported. We match loosely so a
        single check covers the common text-only rejections without
        accidentally catching unrelated 400s (expired keys, over-budget,
        malformed tool schemas, etc).
        """
        if not err_text:
            return False
        lowered = err_text.lower()
        # ``image_url`` is the strongest signal — it's our own content-part
        # type name showing up in the provider's rejection, which only
        # happens when the provider parsed it but didn't recognize the
        # variant.
        if "image_url" in lowered:
            return True
        # Broader fallbacks for providers that don't echo the variant name.
        return any(
            needle in lowered
            for needle in ("does not support image", "vision is not", "vision not supported", "multimodal is not")
        )

    @staticmethod
    def _downgrade_messages_for_text_only(
        messages: list[LLMMessage],
    ) -> list[LLMMessage]:
        """Return a copy of ``messages`` where every user message's image
        attachments are rewritten as inline text placeholders.

        The original messages are not mutated. Non-user roles and messages
        without images pass through untouched. An image-only turn (empty
        ``content`` + images) still emits a readable line so the model has
        *something* to react to.
        """
        out: list[LLMMessage] = []
        for msg in messages:
            if not msg.images:
                out.append(msg)
                continue
            suffix = "\n".join(
                f"[用户附带了图片：{url}]" for url in msg.images if url
            )
            base_text = (msg.content or "").rstrip()
            if base_text and suffix:
                new_content = f"{base_text}\n\n{suffix}"
            else:
                new_content = base_text or suffix
            out.append(LLMMessage(
                role=msg.role,
                content=new_content,
                name=msg.name,
                tool_call_id=msg.tool_call_id,
                tool_calls=msg.tool_calls,
                reasoning_content=msg.reasoning_content,
                images=None,  # critical: prevents the multimodal branch
            ))
        return out

    def _build_api_messages(
        self, sanitized: list[LLMMessage],
    ) -> list[dict[str, Any]]:
        """Serialize messages for the wire, honoring the vision downgrade
        flag. Centralised so retry + first attempt share identical logic.
        """
        if self._vision_unsupported:
            sanitized = self._downgrade_messages_for_text_only(sanitized)
        return [m.to_api() for m in sanitized]

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        temperature: Optional[float] = None,
        session_id: Optional[str] = None,
        stream: Optional[bool] = None,
        # optional async callback invoked on each ``content``
        # delta. Used by AgentLoop's streaming-to-IM dispatcher to push
        # partial assistant text to the gateway while the LLM is still
        # generating. The callback is suppressed automatically the
        # moment a ``tool_calls`` delta is observed; no exception
        # propagates from the callback to the caller (failures are
        # logged at debug level so a flaky IM gateway cannot crash a
        # real LLM response).
        stream_callback: Optional[StreamCallback] = None,
    ) -> LLMResponse:
        if not self.configured:
            raise RuntimeError("LLM is not configured")

        # ZLAgent splits its system prompt on a boundary sentinel so the
        # stable prefix can be cached provider-side (see
        # ``backend.llm.prompt_cache``). The sentinel itself must
        # never reach the provider — strip it right before serialising.
        sanitized = _strip_cache_boundary_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_api_messages(sanitized),
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        # Prompt caching: an identical ``prompt_cache_key`` across turns lets
        # OpenAI-compatible providers reuse the encoded stable prefix
        # instead of re-tokenising it, which materially reduces TTFT and
        # input token cost on subsequent turns of the same session.
        if self.prompt_cache_enabled and session_id:
            payload["prompt_cache_key"] = str(session_id)

        use_stream = self.stream_enabled if stream is None else bool(stream)
        # a stream_callback only makes sense in streaming mode;
        # force stream=True when one is provided so AgentLoop can rely on
        # incremental dispatch even if the operator disabled the global
        # llm_stream_enabled flag.
        if stream_callback is not None:
            use_stream = True
        if use_stream:
            payload["stream"] = True
            # Ensure the terminal chunk carries final usage numbers so we
            # can still surface cache_read / cache_write tokens.
            payload["stream_options"] = {"include_usage": True}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url}/chat/completions"
        logger.debug(
            "llm request provider={} model={} url={} tools={} stream={} cache_key={}",
            self.provider,
            self.model,
            url,
            len(tools or []),
            use_stream,
            bool(payload.get("prompt_cache_key")),
        )
        # v0.40.8 — first attempt may fail with HTTP 400 if the model
        # doesn't support ``image_url`` content parts. On that specific
        # failure, we flip ``_vision_unsupported``, rebuild the payload
        # with images rewritten as text placeholders, and retry ONCE.
        # Any other 400 (bad tool schema, auth, over-budget…) bubbles up
        # unchanged. The retry only activates if the payload actually
        # carried images — callers without attachments don't pay the
        # extra round-trip.
        had_images = any(m.images for m in sanitized)
        try:
            if use_stream:
                data = await self._stream_chat_completion(
                    url, headers, payload, stream_callback=stream_callback,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=True) as client:
                    response = await client.post(url, headers=headers, json=payload)
                if response.status_code >= 400:
                    raise RuntimeError(f"LLM HTTP {response.status_code}: {response.text[:500]}")
                data = response.json()
        except RuntimeError as exc:
            if (
                had_images
                and not self._vision_unsupported
                and self._looks_like_vision_rejection(str(exc))
            ):
                logger.warning(
                    "llm: provider rejected image_url content parts "
                    "(model={}); downgrading to text-only and retrying. "
                    "Future turns will skip images automatically.",
                    self.model,
                )
                self._vision_unsupported = True
                payload["messages"] = self._build_api_messages(sanitized)
                if use_stream:
                    data = await self._stream_chat_completion(
                        url, headers, payload, stream_callback=stream_callback,
                    )
                else:
                    async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=True) as client:
                        response = await client.post(url, headers=headers, json=payload)
                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"LLM HTTP {response.status_code}: {response.text[:500]}"
                        )
                    data = response.json()
            else:
                raise

        content, tool_calls, finish_reason, reasoning = self._extract(data)
        wire_meta = data.get("_zlagent_wire_assistant_text")
        wire_before_scrub = (
            wire_meta if isinstance(wire_meta, str) else (content or "")
        )
        # some providers (DeepSeek V4 Pro observed) emit
        # tool-call intents as XML/DSML text inside ``content`` instead
        # of using the structured ``tool_calls`` field. Those blobs
        # are noise to the user and to the AgentLoop alike; scrub them
        # before doing anything else with the content.
        if content:
            cleaned = strip_hallucinated_tool_calls(content)
            if cleaned != content:
                logger.warning(
                    "llm: scrubbed hallucinated tool-call XML from non-stream"
                    " response ({} → {} chars)",
                    len(content), len(cleaned),
                )
                content = cleaned
        # When the model produced only tool_calls, content is often empty; we
        # must not treat that as a failure — the caller (AgentLoop) will drive
        # a second round-trip after executing the tools.
        if not content and not tool_calls:
            # v0.46 — after scrub, everything may be gone while the wire
            # payload was non-empty (DSML-only "answer"). Raising here made
            # ToolLoopRunner report a false "LLM unreachable" on the final
            # no-tools synthesis pass. Treat as an empty assistant turn.
            if wire_before_scrub.strip():
                logger.warning(
                    "llm: assistant message empty after scrub (had {} wire chars,"
                    " no tool_calls); returning empty content",
                    len(wire_before_scrub.strip()),
                )
            else:
                raise RuntimeError("LLM response contained neither content nor tool_calls")
        data.pop("_zlagent_wire_assistant_text", None)
        usage, cache_read, cache_write = _extract_usage(data)
        # surface real cache hit/miss numbers so operators can
        # tell at a glance whether prompt caching is firing for their
        # provider. DeepSeek reports hit/miss directly; OpenAI reports
        # cached_tokens; Anthropic reports cache_read/cache_creation.
        prompt_total = int(usage.get("prompt_tokens") or 0)
        ds_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        ds_miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        hit_ratio = (cache_read / prompt_total) if prompt_total else 0.0
        logger.info(
            "[perf] llm.usage session={} prompt={} completion={} cache_read={}"
            " cache_write={} hit_ratio={:.0%} (deepseek hit={} miss={})",
            payload.get("prompt_cache_key") or "",
            prompt_total,
            int(usage.get("completion_tokens") or 0),
            cache_read,
            cache_write,
            hit_ratio,
            ds_hit,
            ds_miss,
        )
        return LLMResponse(
            content=(content or "").strip(),
            model=str(data.get("model") or self.model),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning_content=reasoning,
            raw=data,
            usage=usage,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    async def _stream_chat_completion(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        stream_callback: Optional[StreamCallback] = None,
    ) -> dict[str, Any]:
        """Run a Chat Completions request with SSE streaming.

        Returns the same dict shape the non-streaming path returns so the
        downstream ``_extract`` + ``_extract_usage`` path stays single-code.
        Streaming primarily reduces **time-to-first-token**: the provider
        starts emitting tokens while our network/tokeniser pipeline warms up.
        """

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        finish_reason: Optional[str] = None
        model_id: Optional[str] = None
        response_id: Optional[str] = None
        usage: Optional[dict[str, Any]] = None
        # once a tool_calls delta is observed we stop forwarding
        # ``content`` deltas to the IM streaming callback. The same SSE
        # response cannot legally mix a final-text payload with a
        # tool-use payload at the protocol level (OpenAI returns either
        # finish_reason='stop' or 'tool_calls'), but on a misbehaving
        # provider that interleaves the two we'd rather drop a few
        # tokens than send the user a partial answer that is later
        # superseded by a tool call.
        saw_tool_calls = False
        # sibling guard for the case where the model emits an
        # XML/DSML tool-call hallucination inside the ``content`` field
        # (so ``saw_tool_calls`` never fires because no structured
        # ``delta.tool_calls`` ever arrives). Once we see the tripwire
        # token in the accumulating content we suppress further
        # ``stream_callback`` invocations so the IM gateway stops
        # rendering the XML garbage to the user. The aggregated content
        # we return is sanitised after the loop.
        saw_xml_tool_call = False
        # v0.45 — stateful scrubber that survives chunk boundaries.
        # The one-shot ``contains_hallucinated_tool_call`` check above
        # mis-fires when the tripwire is split across deltas (chunk 1
        # = ``< | | ``, chunk 2 = ``DSML | |``), letting chunks 1+2 leak
        # before chunk 3 trips the detector. The scrubber holds back a
        # suspicious tail until it's safe to flush.
        scrubber = StreamingHallucinationScrubber()

        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=True) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise RuntimeError(
                        f"LLM HTTP {response.status_code}:"
                        f" {body.decode('utf-8', 'replace')[:500]}"
                    )
                async for raw_line in response.aiter_lines():
                    if not raw_line:
                        continue
                    if raw_line.startswith(":"):
                        # SSE heartbeat / comment lines — safe to ignore.
                        continue
                    if not raw_line.startswith("data:"):
                        continue
                    payload_line = raw_line[len("data:"):].strip()
                    if not payload_line:
                        continue
                    if payload_line == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload_line)
                    except json.JSONDecodeError:
                        logger.warning(
                            "llm stream: dropping malformed chunk ({} chars)",
                            len(payload_line),
                        )
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("id") and not response_id:
                        response_id = str(chunk["id"])
                    if chunk.get("model") and not model_id:
                        model_id = str(chunk["model"])
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    # ── 1. Tool-calls delta — once we see one, stop the
                    # external content callback so a confused provider
                    # cannot bleed half a user-visible answer into a
                    # tool-driven turn. We still accumulate any prior
                    # content so the LLMResponse we return is a faithful
                    # record of the wire payload.
                    tool_calls_delta = delta.get("tool_calls")
                    if tool_calls_delta:
                        saw_tool_calls = True
                    # ── 2. Visible content delta — push to external IM
                    # streamer first (preserves typing-effect latency)
                    # then accumulate locally for the aggregate return.
                    part = delta.get("content")
                    chunk_texts: list[str] = []
                    if isinstance(part, str) and part:
                        chunk_texts.append(part)
                    elif isinstance(part, list):
                        for item in part:
                            if isinstance(item, dict):
                                text = item.get("text") or item.get("content")
                                if isinstance(text, str) and text:
                                    chunk_texts.append(text)
                    for chunk_text in chunk_texts:
                        # Always accumulate the full wire payload so
                        # the aggregate return reflects what the
                        # provider actually sent (post-aggregation
                        # ``strip_hallucinated_tool_calls`` is the
                        # canonical cleaner for that record).
                        content_parts.append(chunk_text)
                        # v0.45 — stateful scrubber that survives chunk
                        # boundaries. ``feed`` returns the safe-to-emit
                        # slice of this chunk (some trailing fragment
                        # may be held back as a suspicion tail). Once
                        # the scrubber latches on a real tripwire, all
                        # subsequent feeds return ""; we also flip
                        # ``saw_xml_tool_call`` for log-warn parity.
                        visible = scrubber.feed(chunk_text)
                        if scrubber.latched and not saw_xml_tool_call:
                            saw_xml_tool_call = True
                            logger.warning(
                                "llm: hallucinated XML tool-call detected"
                                " in stream; suppressing further user-facing"
                                " deltas (chunk_chars={})",
                                len(chunk_text),
                            )
                        if (
                            visible
                            and stream_callback is not None
                            and not saw_tool_calls
                        ):
                            try:
                                await stream_callback(visible)
                            except Exception as exc:  # noqa: BLE001 - never break LLM on a flaky IM
                                logger.debug(
                                    "stream_callback raised {}; suppressing", exc,
                                )
                    rc = delta.get("reasoning_content")
                    if isinstance(rc, str) and rc:
                        reasoning_parts.append(rc)
                    for tc in tool_calls_delta or []:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index")
                        if not isinstance(idx, int):
                            idx = len(tool_calls_acc)
                        bucket = tool_calls_acc.setdefault(
                            idx,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if tc.get("id"):
                            bucket["id"] = str(tc["id"])
                        fn = tc.get("function") or {}
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                bucket["function"]["name"] = str(fn["name"])
                            args = fn.get("arguments")
                            if isinstance(args, str) and args:
                                bucket["function"]["arguments"] += args

        tool_calls_out: list[dict[str, Any]] = []
        for idx in sorted(tool_calls_acc.keys()):
            entry = tool_calls_acc[idx]
            if not entry["id"]:
                entry["id"] = f"call_{entry['function']['name'] or 'tool'}_{idx}"
            tool_calls_out.append(entry)
        # v0.45 — end-of-stream flush. The scrubber may be holding a
        # suspicious tail that turned out to be benign (e.g. provider
        # ends a turn with ``<f`` but never sends ``unction_calls>``).
        # Surface that tail to the IM gateway before we finalise.
        scrubber_tail = scrubber.flush()
        if (
            scrubber_tail
            and stream_callback is not None
            and not saw_tool_calls
        ):
            try:
                await stream_callback(scrubber_tail)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "stream_callback raised on flush {}; suppressing", exc,
                )
        # strip XML/DSML tool-call hallucinations from the
        # aggregated stream content too. The streaming branch was
        # already protected from leaking new chunks to the user, but
        # the AgentLoop still reads ``LLMResponse.content`` to file
        # the assistant turn into history / memory / wiki cache, so
        # we scrub here for a clean canonical record.
        aggregated_content = "".join(content_parts)
        cleaned_content = strip_hallucinated_tool_calls(aggregated_content)
        if cleaned_content != aggregated_content:
            logger.warning(
                "llm: scrubbed hallucinated tool-call XML from stream"
                " aggregate ({} → {} chars)",
                len(aggregated_content), len(cleaned_content),
            )
        message: dict[str, Any] = {
            "role": "assistant",
            "content": cleaned_content,
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls_out:
            message["tool_calls"] = tool_calls_out
        merged: dict[str, Any] = {
            # Internal: wire assistant text before ``strip_hallucinated_tool_calls``.
            # ``chat()`` uses this to distinguish "provider returned nothing"
            # from "only DSML/XML was returned then scrubbed away" (streaming).
            "_zlagent_wire_assistant_text": aggregated_content,
            "id": response_id,
            "model": model_id or self.model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": message,
                }
            ],
        }
        if usage is not None:
            merged["usage"] = usage
        return merged

    @staticmethod
    def _extract(
        data: dict[str, Any],
    ) -> tuple[str, list[LLMToolCall], Optional[str], Optional[str]]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return "", [], None, None
        first = choices[0]
        if not isinstance(first, dict):
            return "", [], None, None
        finish_reason = first.get("finish_reason")

        message = first.get("message") or {}
        content = LLMClient._flatten_content(message.get("content"))
        tool_calls = LLMClient._parse_tool_calls(message.get("tool_calls"))
        # DeepSeek reasoning models (R1 / V4-Pro thinking mode) emit a
        # ``reasoning_content`` field that *must* be echoed back in the
        # next assistant turn or the endpoint returns HTTP 400.
        reasoning = message.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            reasoning = None
        # Very old providers sometimes put text at the choice level.
        if not content and not tool_calls:
            legacy = first.get("text")
            if isinstance(legacy, str):
                content = legacy
        return content, tool_calls, finish_reason, reasoning

    @staticmethod
    def _flatten_content(raw: Any) -> str:
        if isinstance(raw, str):
            return raw
        if isinstance(raw, list):
            parts: list[str] = []
            for item in raw:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""

    @staticmethod
    def _parse_tool_calls(raw: Any) -> list[LLMToolCall]:
        if not isinstance(raw, list):
            return []
        out: list[LLMToolCall] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") not in (None, "function"):
                # Future OpenAI tool types (e.g. "code_interpreter") — skip.
                continue
            fn = entry.get("function")
            if not isinstance(fn, dict):
                continue
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            arguments = fn.get("arguments")
            if not isinstance(arguments, str):
                # Some providers return an already-parsed dict; re-serialize
                # so the caller always does one consistent json.loads.
                arguments = json.dumps(arguments or {})
            out.append(
                LLMToolCall(
                    id=str(entry.get("id") or f"call_{name}_{len(out)}"),
                    name=name,
                    arguments=arguments,
                )
            )
        return out


def _strip_cache_boundary_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Return a copy of ``messages`` with the cache boundary sentinel removed.

    Only ``system`` messages should carry the sentinel today (the agent's
    assembled system prompt). We still pass every message through the helper
    so future callers cannot accidentally leak the marker into user/tool
    content — it is strictly an internal split point.
    """

    cleaned: list[LLMMessage] = []
    for message in messages:
        content = message.content or ""
        if "<!-- ZLAGENT_CACHE_BOUNDARY -->" in content:
            new_content = strip_system_prompt_cache_boundary(content)
            cleaned.append(
                LLMMessage(
                    role=message.role,
                    content=new_content,
                    name=message.name,
                    tool_call_id=message.tool_call_id,
                    tool_calls=message.tool_calls,
                    reasoning_content=message.reasoning_content,
                )
            )
        else:
            cleaned.append(message)
    return cleaned


def _extract_usage(data: dict[str, Any]) -> tuple[dict[str, int], int, int]:
    """Pull usage numbers and cache counters out of ``data``.

    Three provider conventions are recognised so ``cache_read_tokens`` is
    populated regardless of which OpenAI-compatible backend is in use:

    * **OpenAI (Responses/Completions)** — ``prompt_tokens_details.cached_tokens``
      with ``cache_creation_input_tokens`` for the write-side counter.
    * **Anthropic via OpenAI bridge** — ``cache_read_input_tokens`` /
      ``cache_creation_input_tokens`` at the top of ``usage``.
    * **DeepSeek (api.deepseek.com)** — ``prompt_cache_hit_tokens`` /
      ``prompt_cache_miss_tokens`` at the top of ``usage``. DeepSeek does
      not surface a separate write counter; the miss tokens are an
      indirect proxy.

    Returns ``(usage_dict, cache_read_tokens, cache_write_tokens)``.
    """

    raw = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}, 0, 0
    usage: dict[str, int] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        # DeepSeek-specific counters propagated through so callers /
        # smoke tests can read them directly without knowing the
        # provider.
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ):
        value = raw.get(key)
        if isinstance(value, int):
            usage[key] = value
    cache_read = 0
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if isinstance(cached, int):
            cache_read = cached
    elif isinstance(raw.get("cache_read_input_tokens"), int):
        cache_read = int(raw["cache_read_input_tokens"])
    elif isinstance(raw.get("prompt_cache_hit_tokens"), int):
        # DeepSeek surfaces hit tokens directly. Treat them as
        # cache_read for our internal accounting so the IM
        # gateway / [perf] logs / smoke tests all see one unified
        # ``cache_read_tokens`` field regardless of provider.
        cache_read = int(raw["prompt_cache_hit_tokens"])
    cache_write = 0
    if isinstance(raw.get("cache_creation_input_tokens"), int):
        cache_write = int(raw["cache_creation_input_tokens"])
    if cache_read:
        usage["cache_read_tokens"] = cache_read
    if cache_write:
        usage["cache_write_tokens"] = cache_write
    return usage, cache_read, cache_write
