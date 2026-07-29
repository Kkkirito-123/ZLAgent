"""Durable local JSON CLI for task submission, execution, and control."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Never, TextIO
from uuid import uuid4

from re_zlagent.harness.agent import (
    AgentOrchestrator,
    AgentRunRequest,
    GeneralAgentMode,
    GeneralAgentResult,
    JsonIntentRouter,
)
from re_zlagent.harness.evals import (
    IntentEvalRunner,
    default_intent_stress_corpus_path,
    load_intent_eval_corpus,
)
from re_zlagent.harness.model import (
    ModelClient,
    ModelClientError,
    OpenAICompatibleModelConfig,
)
from re_zlagent.harness.runtime import DurableWorker, RunBranchTreeBuilder

from .bootstrap import (
    ApplicationBootstrapConfig,
    ApplicationContainer,
    ApplicationRuntimeContainer,
    build_application_container,
    build_application_runtime,
)
from .local import LocalTaskAdapter
from .operator import OperatorResponse


class CliArgumentError(ValueError):
    """Raised when CLI arguments are invalid."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Argument parser that lets the CLI return JSON errors."""

    def error(self, message: str) -> Never:
        raise CliArgumentError(message)

    def exit(self, status: int = 0, message: str | None = None) -> Never:
        if status:
            raise CliArgumentError((message or "invalid arguments").strip())
        raise SystemExit(status)


def build_parser() -> argparse.ArgumentParser:
    """Build the local product CLI parser."""

    parser = JsonArgumentParser(prog="zlagent", add_help=True)
    parser.add_argument(
        "--sqlite",
        help="Path to the durable task SQLite database.",
    )
    parser.add_argument(
        "--workspace",
        help="Workspace root exposed to bounded file tools.",
    )
    parser.add_argument(
        "--skill-import-dir",
        help="Controlled local root containing candidate Skill packages.",
    )
    parser.add_argument(
        "--skills-dir",
        help="Managed installed-Skill root; requires --skill-import-dir.",
    )
    parser.add_argument(
        "--mcp-config",
        help="Host-owned JSON config for approved local stdio MCP servers.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for compatibility; output is always JSON.",
    )

    commands = parser.add_subparsers(dest="command")

    ask = commands.add_parser(
        "ask",
        help="Run one general-agent chat or task request.",
    )
    ask.add_argument("goal")
    ask.add_argument(
        "--mode",
        choices=(
            GeneralAgentMode.AUTO.value,
            GeneralAgentMode.CHAT.value,
            GeneralAgentMode.TASK.value,
        ),
        default=GeneralAgentMode.CHAT.value,
        help="Handling mode. Auto task execution stays gated by default.",
    )
    ask.add_argument(
        "--auto-execute-task",
        action="store_true",
        help=(
            "Allow an auto-routed task to enter HarnessRuntime. "
            "Permissions and acceptance still apply."
        ),
    )
    ask.add_argument(
        "--run-id",
        default=None,
        help="Optional request/run identity; generated when omitted.",
    )
    ask.add_argument("--context-json", default="{}")
    ask.add_argument("--prompt-version", default=None)
    _add_model_arguments(ask)

    intent_eval = commands.add_parser(
        "intent-eval",
        help="Measure structured intent-routing accuracy.",
    )
    intent_source = intent_eval.add_mutually_exclusive_group()
    intent_source.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Optional path to a compatible labeled intent corpus.",
    )
    intent_source.add_argument(
        "--stress",
        action="store_true",
        help="Use the packaged mixed and ambiguous routing corpus.",
    )
    _add_model_arguments(intent_eval)

    submit = commands.add_parser("submit", help="Plan and persist a task.")
    submit.add_argument("run_id")
    submit.add_argument("goal")
    submit.add_argument("--context-json", default="{}")
    submit.add_argument("--prompt-version", default=None)
    _add_model_arguments(submit)

    status = commands.add_parser("status", help="Read run progress as JSON.")
    status.add_argument("run_id")

    result = commands.add_parser("result", help="Read persisted task outputs.")
    result.add_argument("run_id")

    branches = commands.add_parser(
        "branches",
        help="Read the fork lineage tree for one run.",
    )
    branches.add_argument("run_id")

    work = commands.add_parser("work", help="Claim and execute durable work.")
    work.add_argument("run_id", nargs="?", default=None)
    work.add_argument("--worker-id", default="local-worker")
    work.add_argument("--lease-seconds", type=int, default=30)
    work.add_argument("--retry-budget", type=int, default=3)
    work.add_argument("--base-backoff-seconds", type=int, default=5)
    work.add_argument("--max-backoff-seconds", type=int, default=300)
    work.add_argument(
        "--until-idle",
        action="store_true",
        help="Process claimable runs until none remain.",
    )
    work.add_argument("--max-ticks", type=int, default=100)

    approve = commands.add_parser(
        "approve",
        help="Approve and resume a waiting-user checkpoint.",
    )
    approve.add_argument("run_id")
    approve.add_argument("--checkpoint-id", default=None)
    approve.add_argument("--resume-token", default=None)
    approve.add_argument("--feedback", default="")

    pause = commands.add_parser("pause", help="Pause a run.")
    pause.add_argument("run_id")
    pause.add_argument("--reason", default="")
    pause.add_argument("--actor", default="user")

    resume = commands.add_parser("resume", help="Resume a paused or blocked run.")
    resume.add_argument("run_id")
    resume.add_argument("feedback", nargs="?", default="")
    resume.add_argument("--actor", default="user")

    cancel = commands.add_parser("cancel", help="Cancel a run.")
    cancel.add_argument("run_id")
    cancel.add_argument("--reason", default="")
    cancel.add_argument("--actor", default="user")

    fork = commands.add_parser("fork", help="Fork a run into a new run.")
    fork.add_argument("run_id")
    fork.add_argument("new_run_id")
    fork.add_argument("--reason", default="")
    fork.add_argument("--actor", default="user")
    fork.add_argument("--checkpoint-id", default=None)

    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    model: ModelClient | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the CLI and return a process-style exit code."""

    out = stdout or sys.stdout
    _ = stderr or sys.stderr
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
        if not getattr(args, "command", None):
            raise CliArgumentError("command is required")
        sqlite_path = getattr(args, "sqlite", None)
        if args.command not in {"ask", "intent-eval"} and not sqlite_path:
            raise CliArgumentError("--sqlite is required")
        data, code = asyncio.run(
            _run_command(
                args,
                sqlite_path=Path(sqlite_path) if sqlite_path else None,
                injected_model=model,
                environ=environ,
            )
        )
        _write_json(
            data,
            stdout=out,
            pretty=bool(getattr(args, "pretty", False)),
        )
        return code
    except CliArgumentError as exc:
        return _write_error(out, "invalid_arguments", str(exc), code=2)
    except ModelClientError as exc:
        return _write_error(out, "model_error", str(exc), code=1)
    except ValueError as exc:
        return _write_error(out, "value_error", str(exc), code=1)
    except RuntimeError as exc:
        return _write_error(out, "runtime_error", str(exc), code=1)
    except Exception as exc:  # noqa: BLE001 - CLI failures are structured data
        return _write_error(
            out,
            "internal_error",
            f"{type(exc).__name__}: {exc}",
            code=1,
        )


async def _run_command(
    args: argparse.Namespace,
    *,
    sqlite_path: Path | None,
    injected_model: ModelClient | None,
    environ: Mapping[str, str] | None,
) -> tuple[dict[str, Any], int]:
    config = ApplicationBootstrapConfig(
        workspace_dir=(Path(args.workspace) if args.workspace else None),
        sqlite_path=sqlite_path,
        register_file_tools=bool(args.workspace),
        skill_import_dir=(
            Path(args.skill_import_dir) if args.skill_import_dir else None
        ),
        skills_dir=Path(args.skills_dir) if args.skills_dir else None,
        mcp_config_path=Path(args.mcp_config) if args.mcp_config else None,
        allow_auto_task_execution=bool(
            getattr(args, "auto_execute_task", False)
        ),
    )
    command = str(args.command)
    if command == "intent-eval":
        resolved_model = injected_model or _model_from_args(args, environ=environ)
        corpus = load_intent_eval_corpus(
            default_intent_stress_corpus_path()
            if args.stress
            else args.corpus
        )
        report = await IntentEvalRunner(
            JsonIntentRouter(resolved_model),
            corpus,
        ).run()
        payload = report.to_dict()
        payload["command"] = "intent-eval"
        return payload, 0 if report.ok else 1

    if command == "ask":
        resolved_model = injected_model or _model_from_args(args, environ=environ)
        container = build_application_container(
            model=resolved_model,
            config=config,
            environ=environ,
        )
        try:
            request = AgentRunRequest(
                run_id=args.run_id or f"ask-{uuid4().hex}",
                user_goal=args.goal,
                context=_parse_context(args.context_json),
                model_name=args.model or getattr(resolved_model, "model", None),
                prompt_version=args.prompt_version,
            )
            result = await container.general_agent.run(
                request,
                mode=args.mode,
            )
            return _general_agent_result_to_dict(result), 0
        finally:
            container.close()

    if command == "submit":
        resolved_model = injected_model or _model_from_args(args, environ=environ)
        container = build_application_container(
            model=resolved_model,
            config=config,
            environ=environ,
        )
        try:
            adapter = _local_adapter(
                container,
                worker_id="submit-worker",
                orchestrator=container.orchestrator,
            )
            context = _parse_context(args.context_json)
            response = await adapter.submit(
                AgentRunRequest(
                    run_id=args.run_id,
                    user_goal=args.goal,
                    context=context,
                    model_name=args.model or getattr(resolved_model, "model", None),
                    prompt_version=args.prompt_version,
                )
            )
            return response, 0
        finally:
            container.close()

    services = build_application_runtime(config=config, environ=environ)
    try:
        worker = DurableWorker(
            worker_id=getattr(args, "worker_id", "local-worker"),
            store=services.store,
            runtime=services.runtime,
            lease_seconds=getattr(args, "lease_seconds", 30),
            retry_budget=getattr(args, "retry_budget", 3),
            base_backoff_seconds=getattr(args, "base_backoff_seconds", 5),
            max_backoff_seconds=getattr(args, "max_backoff_seconds", 300),
        )
        adapter = LocalTaskAdapter(
            store=services.store,
            long_task_store=services.long_task_store,
            operator=services.operator,
            approvals=services.approvals,
            worker=worker,
        )
        if command == "status":
            response = adapter.status(args.run_id)
            return response, 0 if response["ok"] else 1
        if command == "result":
            response = adapter.result(args.run_id)
            return response, 0 if response["ok"] else 1
        if command == "branches":
            tree = RunBranchTreeBuilder(services.store).build(args.run_id)
            return {
                "ok": True,
                "command": "branches",
                "tree": tree.to_dict(),
            }, 0
        if command == "work":
            return await _work(adapter, args)
        if command == "approve":
            response = await adapter.approve(
                args.run_id,
                checkpoint_id=args.checkpoint_id,
                resume_token=args.resume_token,
                feedback=args.feedback,
            )
            return response, 0 if response["ok"] else 2
        control_response = _run_control(services, args)
        return control_response.to_dict(), _operator_exit_code(control_response)
    finally:
        services.close()


async def _work(
    adapter: LocalTaskAdapter,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], int]:
    if args.max_ticks <= 0:
        raise CliArgumentError("--max-ticks must be positive")
    ticks: list[dict[str, Any]] = []
    for _ in range(args.max_ticks if args.until_idle else 1):
        tick = await adapter.work_once(args.run_id)
        ticks.append(tick)
        if not args.until_idle:
            return tick, 0 if tick["ok"] else 1
        if args.run_id is not None or tick["status"] in {
            "no_candidate",
            "retry_scheduled",
            "dead_letter",
            "lease_lost",
        }:
            break
    response = {
        "ok": all(tick["ok"] for tick in ticks),
        "command": "work",
        "until_idle": True,
        "tick_count": len(ticks),
        "ticks": ticks,
        "stopped_status": ticks[-1]["status"],
        "limit_reached": (
            len(ticks) == args.max_ticks and ticks[-1]["status"] != "no_candidate"
        ),
    }
    return response, 0 if response["ok"] else 1


def _run_control(
    services: ApplicationRuntimeContainer,
    args: argparse.Namespace,
) -> OperatorResponse:
    command = str(args.command)
    if command == "pause":
        return services.operator.pause(
            args.run_id,
            reason=args.reason,
            actor=args.actor,
        )
    if command == "resume":
        return services.operator.resume(
            args.run_id,
            feedback=args.feedback,
            actor=args.actor,
        )
    if command == "cancel":
        return services.operator.cancel(
            args.run_id,
            reason=args.reason,
            actor=args.actor,
        )
    if command == "fork":
        return services.operator.fork(
            args.run_id,
            new_run_id=args.new_run_id,
            reason=args.reason,
            actor=args.actor,
            checkpoint_id=args.checkpoint_id,
        )
    raise CliArgumentError(f"unknown command: {command}")


def _local_adapter(
    container: ApplicationContainer,
    *,
    worker_id: str,
    orchestrator: AgentOrchestrator | None = None,
) -> LocalTaskAdapter:
    worker = DurableWorker(
        worker_id=worker_id,
        store=container.store,
        runtime=container.runtime,
    )
    return LocalTaskAdapter(
        store=container.store,
        long_task_store=container.long_task_store,
        operator=container.operator,
        approvals=container.approvals,
        worker=worker,
        orchestrator=orchestrator,
    )


def _model_from_args(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None,
) -> ModelClient:
    values = os.environ if environ is None else environ
    base_url = args.base_url or values.get("ZLAGENT_MODEL_BASE_URL")
    model_name = args.model or values.get("ZLAGENT_MODEL_NAME")
    api_key_env = (
        args.api_key_env or values.get("ZLAGENT_API_KEY_ENV") or "OPENAI_API_KEY"
    )
    max_tokens = _positive_optional_int(
        args.max_output_tokens,
        values.get("ZLAGENT_MODEL_MAX_TOKENS"),
        label="model max output tokens",
    )
    if not base_url:
        raise CliArgumentError(
            "--base-url or ZLAGENT_MODEL_BASE_URL is required"
        )
    if not model_name:
        raise CliArgumentError("--model or ZLAGENT_MODEL_NAME is required")
    config = OpenAICompatibleModelConfig(
        base_url=base_url,
        model=model_name,
        api_key_env=None if args.no_api_key else api_key_env,
        timeout_seconds=args.timeout_seconds,
        max_tokens=max_tokens,
    )
    return config.build_client(environ=environ)


def _add_model_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        default=None,
        help="OpenAI-compatible base URL; or ZLAGENT_MODEL_BASE_URL.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Provider model name; or ZLAGENT_MODEL_NAME.",
    )
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument(
        "--api-key-env",
        default=None,
        help="Environment variable containing the API key.",
    )
    auth.add_argument(
        "--no-api-key",
        action="store_true",
        help="Explicitly use an unauthenticated local provider.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help=(
            "Hard provider output ceiling; or ZLAGENT_MODEL_MAX_TOKENS. "
            "Per-phase harness budgets may lower it."
        ),
    )


def _general_agent_result_to_dict(result: GeneralAgentResult) -> dict[str, Any]:
    task: dict[str, Any] | None = None
    if result.agent_result is not None:
        runtime_result = result.agent_result.runtime_result
        decision = runtime_result.acceptance_decision
        task = {
            "status": runtime_result.run.status.value,
            "accepted": runtime_result.accepted,
            "current_checkpoint_id": runtime_result.run.current_checkpoint_id,
            "tool_result_count": len(runtime_result.tool_results),
            "checkpoint_count": len(runtime_result.checkpoints),
            "acceptance": (
                {
                    "status": decision.status.value,
                    "reason": decision.reason,
                    "evidence_refs": list(decision.evidence_refs),
                }
                if decision is not None
                else None
            ),
            "failure": (
                runtime_result.failure.to_dict()
                if runtime_result.failure is not None
                else None
            ),
        }
    return {
        "ok": True,
        "command": "ask",
        "mode": result.mode.value,
        "run_id": result.request.run_id,
        "response": result.response,
        "verified": result.verified,
        "intent": (
            {
                "route": result.intent_decision.route.value,
                "reason_code": result.intent_decision.reason_code.value,
                "clarification_question": (
                    result.intent_decision.clarification_question
                ),
            }
            if result.intent_decision is not None
            else None
        ),
        "context_manifest": (
            result.context_manifest.to_dict()
            if result.context_manifest is not None
            else None
        ),
        "memory_capture": (
            result.memory_capture.to_dict()
            if result.memory_capture is not None
            else None
        ),
        "response_metadata": dict(result.response_metadata),
        "token_usage": result.token_usage,
        "task": task,
    }


def _positive_optional_int(
    argument_value: int | None,
    environment_value: str | None,
    *,
    label: str,
) -> int | None:
    if argument_value is not None:
        value = argument_value
    elif environment_value is not None:
        try:
            value = int(environment_value)
        except ValueError as exc:
            raise CliArgumentError(f"{label} must be an integer") from exc
    else:
        return None
    if value <= 0:
        raise CliArgumentError(f"{label} must be positive")
    return value


def _parse_context(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliArgumentError(f"--context-json is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CliArgumentError("--context-json must contain a JSON object")
    return value


def _operator_exit_code(response: OperatorResponse) -> int:
    if response.ok:
        return 0
    if response.command == "status":
        return 1
    return 2


def _write_error(stdout: TextIO, error_type: str, message: str, *, code: int) -> int:
    _write_json(
        {
            "ok": False,
            "error": {
                "type": error_type,
                "message": message,
            },
        },
        stdout=stdout,
        pretty=False,
    )
    return code


def _write_json(data: dict[str, Any], *, stdout: TextIO, pretty: bool) -> None:
    stdout.write(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2 if pretty else None,
            sort_keys=True,
        )
    )
    stdout.write("\n")


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
