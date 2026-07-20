"""Runtime configuration for ZLAgent.

Values are read from environment variables (prefixed with ZLAGENT_) and,
optionally, a local .env file loaded by pydantic-settings. This module is the
single source of truth for paths, ports and feature flags used by the rest of
the backend.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_prefix="ZLAGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application metadata
    app_name: str = "ZLAgent"
    version: str = "1.2.2"

    # HTTP server
    host: str = "0.0.0.0"
    port: int = 8020
    log_level: str = "INFO"

    # Paths (defaults match the Docker layout; override for local dev)
    data_dir: Path = Field(default=Path("/app/data"))
    config_dir: Path = Field(default=Path("/app/config"))
    workspace_dir: Path = Field(default=Path("/app/workspace"))

    # Database
    database_url: str = Field(
        default="sqlite:////app/data/zlagent.db",
        validation_alias=AliasChoices("DATABASE_URL", "ZLAGENT_DATABASE_URL"),
    )

    # Model provider hints (not bound to a specific vendor)
    default_model: Optional[str] = None
    llm_provider: str = "openai-compatible"
    llm_timeout_seconds: float = 60.0
    llm_temperature: float = 0.4
    # bumped 4000 → 8000. DeepSeek V3/V4 caps output at 8k
    # tokens. Multi-tool turns (skill_manage write + rich-card travel guide
    # + 10-question agent push) routinely emit 2-5k tokens of structured
    # content before the assistant text; capping at 4k silently truncated
    # the user-visible reply tail. 8000 sits at the model's hard ceiling
    # so we stop leaving headroom unused.
    llm_max_tokens: int = 8000
    llm_context_window_tokens: int = 128_000

    # speed mechanisms ported from openclaw.
    #
    # ``prompt_cache_enabled`` controls whether each chat request carries
    # a ``prompt_cache_key`` derived from the session id. OpenAI-compatible
    # providers can then reuse the encoded stable system prefix across
    # turns, reducing TTFT and input token cost. Some self-hosted gateways
    # reject unknown payload keys — disable the flag if a provider 400s.
    #
    # ``llm_stream_enabled`` switches the chat client to SSE streaming so
    # the provider can emit tokens while our network/tokeniser pipeline
    # warms up. We still aggregate the full message before returning, so
    # callers see the same shape — only TTFT changes.
    prompt_cache_enabled: bool = True
    llm_stream_enabled: bool = True

    # second-LLM router. A small, fast model decides which
    # knowledge_mode + skill best fits the user message and the
    # AgentLoop injects that hint into the system prompt before
    # handing off to the main (Pro) model. Default-off so existing
    # deployments behave unchanged. Set ``router_llm_enabled=true``
    # plus ``router_llm_model`` (e.g. ``deepseek-v4-flash``) to turn
    # it on. The router reuses the main provider's base_url / api_key
    # unless explicitly overridden, so on DeepSeek you only need the
    # model name + an enable flag.
    #
    # On router failure (timeout / non-JSON / provider 5xx) we
    # fail-soft: the router returns an empty decision and the Pro
    # model runs against the unchanged prompt. Router latency is
    # bounded by ``router_llm_timeout_seconds`` (default 1.5 s) so
    # a slow router never blocks the user-visible response for long.
    router_llm_enabled: bool = False
    router_llm_model: str = ""
    router_llm_base_url: Optional[str] = None
    router_llm_api_key: Optional[str] = None
    router_llm_timeout_seconds: float = 1.5
    router_llm_cache_ttl_seconds: float = 60.0

    # token-level streaming dispatch to IM. When the LLM emits a
    # final assistant reply (no tool_calls), partial text is pushed to the
    # gateway every ``flush_chars`` characters or ``flush_interval_ms``
    # milliseconds, whichever comes first. Result: users see the typing
    # effect on flushable channels (multi-message on微信 / QQ; future
    # ``edit_message`` paths on 飞书 / Telegram). Set
    # ``agent_stream_to_im_enabled=False`` to keep the v0.37.8 behaviour
    # (single send at the end). The thresholds are conservative — too
    # small floods IM rate-limits, too large defeats the point.
    agent_stream_to_im_enabled: bool = True
    agent_stream_flush_chars: int = 200
    agent_stream_flush_interval_ms: int = 1500
    agent_stream_min_chars: int = 80

    # v0.9 skill self-review (post-turn introspection that may patch / create
    # skills the agent just learned from). Tuning rationale:
    #   * ``min_steps=2`` — every turn that involved >=2 tool calls is
    #     considered "substantive" enough to be worth reviewing. Pure echo
    #     turns and zero-tool DM exchanges skip the review entirely.
    #   * ``max_iterations=3`` — the review prompt is narrow (only
    #     ``skill_manage`` is in scope), so 3 LLM round-trips is plenty;
    #     higher would mostly waste tokens on the model second-guessing
    #     itself.
    agent_review_enabled: bool = True
    agent_review_min_steps: int = 2
    agent_review_max_iterations: int = 3

    # v0.11 skill curator (long-horizon background review). Defaults match
    # Hermes' conservative tuning — a 5-minute warmup (so the rest of the
    # FastAPI app finishes booting first), 12-hour interval, 30 days for
    # stale, 90 days for archive-candidate.
    skill_curator_enabled: bool = True
    skill_curator_warmup_seconds: int = 300
    skill_curator_interval_seconds: int = 12 * 3600
    skill_curator_stale_after_days: int = 30
    skill_curator_archive_after_days: int = 90
    skill_curator_dedupe_threshold: float = 0.70

    # skill knowledge consolidator. Walks the skill manifests on
    # a slow cadence and emits a derived ``workspace/knowledge/wiki/``
    # tree (concept pages + ``[[wikilinks]]`` graph) compatible with the
    # ``llm_wiki`` desktop app. Pure file I/O — no LLM, no embeddings —
    # so the default cadence is conservative (10-minute warmup,
    # 24-hour interval) on the assumption skills change rarely.
    skill_knowledge_enabled: bool = True
    skill_knowledge_warmup_seconds: int = 600
    skill_knowledge_interval_seconds: int = 24 * 3600
    skill_knowledge_dir: Optional[Path] = None
    skill_knowledge_max_body_chars: int = 2000

    # v0.43 Phase B daily review (end-of-day / phase-completion 复盘).
    # Drives the END_OF_DAY_REVIEW_PROMPT review fork on a slow cadence
    # so the agent gets a chance to consolidate the day's L2 agent_note
    # writes into proper SKILL.md files (the user's 钱学森 "skill 延迟
    # 梳理" doctrine). Defaults: wait 1 h after boot, then fire every
    # 24 h, looking back 24 h. Operator can disable via env or trigger
    # an out-of-band run via REST.
    daily_review_enabled: bool = True
    daily_review_warmup_seconds: int = 3600
    daily_review_interval_seconds: int = 24 * 3600
    daily_review_lookback_seconds: int = 24 * 3600
    daily_review_max_iterations: int = 8
    # When set to an existing ``delivery_targets`` row id, the service
    # pushes a formatted daily-report message to that target after a
    # successful run. Leave ``None`` (default) for silent operation —
    # operator can read /api/review/state on demand instead.
    daily_review_push_target_id: Optional[int] = None

    # v0.12 cross-session memory. Defaults are conservative — designed for
    # a single-user personal-assistant deployment where the operator only
    # sees ~10-50 facts about themselves persisted at any time. Bump the
    # ceilings (and the per-prompt caps in MemoryManager) once you actually
    # hit them; until then a tight budget keeps the system prompt cheap.
    memory_max_entries: int = 200
    memory_max_entry_chars: int = 500
    memory_max_user_facts_in_prompt: int = 30
    memory_max_agent_notes_in_prompt: int = 30
    memory_max_prefetch_results: int = 5
    memory_session_context_enabled: bool = True
    memory_session_context_max_turns: int = 12
    memory_session_context_max_chars: int = 8000
    memory_session_context_token_budget_ratio: float = 0.04
    # v1.2 — LLM-driven knowledge-graph extraction. The synchronous request
    # path only ever consumes already-cached results, so a slow/down LLM
    # cannot block /api/knowledge-bases. Cache writes happen either
    # asynchronously after a successful ingest or via the explicit
    # ``POST /api/knowledge-bases/{id}/rebuild-graph`` endpoint.
    graph_llm_enabled: bool = True
    graph_llm_timeout_seconds: float = 8.0
    graph_llm_max_chars: int = 4000
    graph_node_limit_per_kb: int = 80
    # v0.45 — Hermes-style memory: prefer the static system-prompt
    # snapshot (``system_prompt_block``) and disable the per-turn
    # keyword search that was responsible for cross-context bleed-
    # through (e.g. typing "7 天" recalling a stored "上海→北京 7 日
    # 攻略"). Set ``True`` to revert to the v0.43 substring + char-gram
    # ranking; only useful when memory grows past a couple hundred
    # entries AND the LLM stops being able to consult the full snapshot.
    memory_keyword_prefetch_enabled: bool = False
    # Hard cap on a single memory entry's content. Hermes' built-in
    # store keeps each fact under ~Twitter-length so the snapshot
    # block stays cheap and curated. Writes exceeding this length are
    # rejected by ``memory_manage`` with a hint to summarise — this
    # is the guard that prevents the LLM from saving "整份 7 日规划"
    # into a fact entry.
    memory_max_fact_chars: int = 280

    # v0.45 — main-turn tool budget. Used to be hard-coded at 4 in
    # ``AgentLoop.__init__`` which felt too tight on "find me 5 papers"-
    # style multi-step asks (logs showed agents bouncing off the cap
    # at step 4 and falling back to verbatim text). Bumped the default
    # and exposed the env override so power users can dial without
    # touching code. The adaptive bump (see ``adaptive_tool_iterations``
    # below) further raises it for explicit list-style requests.
    agent_max_tool_iterations: int = 6
    # When a user message looks like a list-collection task ("找 5 篇
    # 论文", "对比 3 个候选", "综述", "SOTA", "顶刊"), bump the budget
    # to this value for that turn only. Bigger is safer for these
    # asks; the guardrail's repeated-failure halter still catches
    # runaway loops.
    agent_max_tool_iterations_list_task: int = 10

    # failure-pattern tracker. Once a (tool, error_signature)
    # fingerprint has fired this many times across distinct turns, the
    # FailureLearner auto-writes one ``agent_note`` describing the
    # pattern. Higher → quieter; 3 is conservative and matches Hermes'
    # informal threshold for "yeah this is recurring".
    failure_learning_enabled: bool = True
    failure_learning_threshold: int = 3

    # MCP (Model Context Protocol) client. Disabled by default; the
    # operator opts in by writing to ``mcp_config_path``. Path defaults to
    # ``<config_dir>/mcp_servers.yaml`` so it lives next to the rest of the
    # mounted config volume in the docker layout.
    mcp_enabled: bool = True
    mcp_config_path: Optional[Path] = None
    mcp_breaker_threshold: int = 5
    mcp_breaker_cooldown_seconds: float = 60.0

    # trajectory compression. When ``run_turn`` history grows past
    # ``trajectory_max_chars`` (rough proxy for token usage at ≈4 chars per
    # token), we compress in two phases: trim older tool outputs first,
    # then collapse middle rounds into a single summary system message.
    # Disabled by default for tests; enabled in production.
    #
    # raised the trajectory budget from 32 KB (≈8k tokens) to
    # 256 KB (≈64k tokens). The old cap meant a 128k-window model like
    # DeepSeek V4 Pro / Claude Sonnet started compressing at 6 % usage,
    # which destroyed multi-turn coherence (the user saw the assistant
    # "forget" tool results from the same conversation). 256 KB leaves a
    # comfortable half-window for the system prompt + active tool round
    # + reply, and ContextEngine still has the LLM-summary phase below
    # for the rare turns that overflow further.
    trajectory_compress_enabled: bool = True
    trajectory_max_chars: int = 256 * 1024
    trajectory_keep_recent_rounds: int = 4
    trajectory_keep_last_tool_results: int = 3
    trajectory_truncated_tool_chars: int = 200

    # tool-loop guardrail. Detects three pathological patterns
    # inside a single agent turn (repeated identical failure, same tool
    # repeated failure across args, read-only no-progress) and surfaces
    # the loop hint to the LLM.
    #
    # ``hard_stop_enabled`` keeps the controller in observation+warn mode
    # by default — it never refuses a tool call, only annotates results
    # with the loop guidance. Flip it on once the thresholds have been
    # tuned for your real workload.
    tool_guardrails_enabled: bool = True
    # v0.45 — was False; was responsible for "DDG fails 4x, LLM still
    # retries DDG till max_iter" loops. Now True by default; the halt
    # threshold (``halt_after_same_tool_failure``, default 8) is high
    # enough that legitimate retries still pass while runaway loops
    # are short-circuited.
    tool_guardrails_hard_stop_enabled: bool = True
    tool_guardrails_warn_after_failure: int = 2
    tool_guardrails_block_after_failure: int = 4
    tool_guardrails_warn_after_same_tool_failure: int = 2
    # v0.45 — dropped from 8 → 4. With ``agent_max_tool_iterations=6``
    # a halt threshold of 8 was unreachable: the loop hit max-iter
    # before the halter ever fired. 4 lets the controller stop a
    # runaway tool early while still permitting normal retry-once
    # recoveries.
    tool_guardrails_halt_after_same_tool_failure: int = 4
    tool_guardrails_warn_after_no_progress: int = 2
    tool_guardrails_block_after_no_progress: int = 4

    # v1.2.0 — bounded concurrency for parallel-capable tool calls.
    # When the LLM emits multiple read-only safe tools in a single
    # assistant step (e.g. parallel ``read_url``s or ``web_search``es),
    # the runner now fires them concurrently up to this cap. Set to 1
    # to fall back to fully sequential execution. Defaults to 4 because
    # most real workloads only ever batch 2-3 read calls and external
    # rate limits start to bite past ~5. Tool concurrency_safe + read-only
    # metadata is still the source of truth for *which* tools qualify;
    # this cap only bounds the fan-out width.
    tool_loop_parallel_max_concurrency: int = 4

    # SkillGuard static security scanner. Runs on every
    # ``skill_manage`` create / edit / patch / write_file *before* the
    # write hits disk. ``strict_for_agent`` makes the autonomous review
    # fork (background_review) refuse caution-class findings too — the
    # operator can't see what the fork wrote in real time, so we hold
    # it to a tighter bar than user-driven foreground writes.
    skill_guard_enabled: bool = True
    skill_guard_strict_for_agent: bool = True

    # LLM-summary phase for ContextEngine. Sits between the
    # cheap micro-compaction layer and the deterministic round-drop
    # fallback. Only fires when ``context_summary_threshold_chars`` is
    # exceeded *after* micro-compaction, so trivial overshoots stay on
    # the cheap path and never burn LLM quota.
    #
    # raised threshold 48 KB (≈12k tokens) → 384 KB (≈96k
    # tokens). The previous threshold fired at 9 % of a 128k window
    # which spent an LLM call on summarisation every few turns. 384 KB
    # holds the LLM summary back until we're at ≈75 % window usage,
    # which is where the cost-of-summary becomes worth paying.
    context_summary_enabled: bool = True
    context_summary_threshold_chars: int = 384 * 1024
    context_summary_max_chars: int = 4 * 1024
    context_summary_failure_cooldown_seconds: float = 60.0

    # local-directory plugin loader. Disabled by default —
    # plugins run in the same process as ZLAgent and can do anything
    # the host can, so the operator must opt in explicitly. Path
    # defaults to ``<workspace_dir>/plugins/``.
    plugins_enabled: bool = False
    plugins_dir: Optional[Path] = None

    # concurrency model. The gateway manager enforces a per-user
    # semaphore around agent handler invocations. Different users always
    # run in parallel; the same (platform, user_id) tuple is allowed at
    # most ``per_user_concurrency`` simultaneous turns. A 4th message
    # queues FIFO until a slot frees. Choose this carefully: too low and
    # users feel the bot "lags" behind their typing; too high and a
    # single user can pin LLM tokens and memory. 3 is a sweet spot for
    # one-on-one IM.
    per_user_concurrency: int = Field(
        default=3,
        ge=1,
        le=64,
        validation_alias=AliasChoices(
            "ZLAGENT_PER_USER_CONCURRENCY", "PER_USER_CONCURRENCY"
        ),
    )
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_API_KEY", "ZLAGENT_OPENAI_API_KEY"),
    )
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("OPENAI_BASE_URL", "ZLAGENT_OPENAI_BASE_URL"),
    )
    openai_model: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_MODEL", "ZLAGENT_OPENAI_MODEL"),
    )

    # v0.6: confirmation flow tuning. ``confirmation_ttl_seconds`` controls how
    # long a pending confirmation stays open before the periodic sweeper marks
    # it expired. Five minutes is the smallest value that still feels
    # forgiving for a human reading the bot's question on their phone.
    confirmation_ttl_seconds: int = 300

    # Channel adapter feature flags. Concrete credentials live in .env and are
    # read by each adapter on its own, keeping the core config minimal.
    wecom_enabled: bool = False
    feishu_enabled: bool = False
    email_enabled: bool = False
    telegram_enabled: bool = False

    # Redis-backed temporary storage. ``redis_url`` empty means
    # "disabled"; every caller transparently falls back to the existing
    # in-memory + JSONL/SQLite layers. Set to e.g. ``redis://redis:6379/0``
    # in docker-compose so multiple workers share session context, travel
    # bundle cache, and the wiki answer cache. TTLs are per-namespace so
    # session turns can survive a user idling through the night while a
    # 12306 bundle stays fresh for only ~60s.
    redis_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ZLAGENT_REDIS_URL", "REDIS_URL",
        ),
    )
    redis_key_prefix: str = "zlagent"
    redis_connect_timeout_seconds: float = 2.0
    redis_command_timeout_seconds: float = 1.0
    redis_session_ttl_seconds: int = 24 * 3600
    redis_bundle_ttl_seconds: int = 60
    redis_wiki_ttl_seconds: int = 30 * 86_400

    def ensure_directories(self) -> None:
        """Create the runtime directories if they do not exist."""
        for path in (self.data_dir, self.config_dir, self.workspace_dir):
            path.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "skills").mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "memory").mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "files").mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "logs").mkdir(parents=True, exist_ok=True)
        knowledge_dir = self.skill_knowledge_dir or (self.workspace_dir / "knowledge")
        knowledge_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so importing modules share a single Settings instance."""
    return Settings()
