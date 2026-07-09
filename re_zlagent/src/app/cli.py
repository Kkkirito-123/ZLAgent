"""JSON CLI surface for re_zlagent operator controls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

from harness.progress import ProgressStatus, TaskProgressReader
from harness.runtime import RunControlResult, RunControlService
from harness.storage import SqliteTaskStore
from harness.storage.serde import checkpoint_to_dict, event_to_dict, run_to_dict


class CliArgumentError(ValueError):
    """Raised when CLI arguments are invalid."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Argument parser that lets the CLI return JSON errors."""

    def error(self, message: str) -> None:
        raise CliArgumentError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            raise CliArgumentError((message or "invalid arguments").strip())
        raise SystemExit(status)


def build_parser() -> argparse.ArgumentParser:
    """Build the operator CLI parser."""

    parser = JsonArgumentParser(prog="zlagent", add_help=True)
    parser.add_argument(
        "--sqlite",
        help="Path to the durable task SQLite database.",
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

    status = commands.add_parser("status", help="Read run progress as JSON.")
    status.add_argument("run_id")

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
) -> int:
    """Run the CLI and return a process-style exit code."""

    out = stdout or sys.stdout
    _ = stderr or sys.stderr
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
        if not getattr(args, "command", None):
            raise CliArgumentError("command is required")
        sqlite_path = getattr(args, "sqlite", None)
        if not sqlite_path:
            raise CliArgumentError("--sqlite is required")
        return _run_with_store(args, Path(sqlite_path), out)
    except CliArgumentError as exc:
        _write_json(
            {
                "ok": False,
                "error": {
                    "type": "invalid_arguments",
                    "message": str(exc),
                },
            },
            stdout=out,
            pretty=False,
        )
        return 2
    except ValueError as exc:
        _write_json(
            {
                "ok": False,
                "error": {
                    "type": "value_error",
                    "message": str(exc),
                },
            },
            stdout=out,
            pretty=False,
        )
        return 1


def _run_with_store(args: argparse.Namespace, sqlite_path: Path, stdout: TextIO) -> int:
    store = SqliteTaskStore(sqlite_path)
    try:
        pretty = bool(getattr(args, "pretty", False))
        command = str(args.command)
        if command == "status":
            snapshot = TaskProgressReader(store).snapshot(args.run_id)
            ok = snapshot.status is not ProgressStatus.MISSING
            _write_json(
                {
                    "ok": ok,
                    "command": command,
                    "run_id": args.run_id,
                    "progress": snapshot.to_dict(),
                },
                stdout=stdout,
                pretty=pretty,
            )
            return 0 if ok else 1

        control = RunControlService(store)
        if command == "pause":
            result = control.pause(
                args.run_id,
                reason=args.reason,
                actor=args.actor,
            )
        elif command == "resume":
            result = control.resume(
                args.run_id,
                feedback=args.feedback,
                actor=args.actor,
            )
        elif command == "cancel":
            result = control.cancel(
                args.run_id,
                reason=args.reason,
                actor=args.actor,
            )
        elif command == "fork":
            result = control.fork(
                args.run_id,
                new_run_id=args.new_run_id,
                reason=args.reason,
                actor=args.actor,
                checkpoint_id=args.checkpoint_id,
            )
        else:
            raise CliArgumentError(f"unknown command: {command}")

        _write_json(
            {
                "ok": result.accepted,
                "command": command,
                "result": _control_result_to_dict(result),
            },
            stdout=stdout,
            pretty=pretty,
        )
        return 0 if result.accepted else 2
    finally:
        store.close()


def _control_result_to_dict(result: RunControlResult) -> dict[str, Any]:
    return {
        "accepted": result.accepted,
        "action": result.action.value,
        "reason": result.reason,
        "run": run_to_dict(result.run),
        "event": event_to_dict(result.event) if result.event else None,
        "checkpoint": (
            checkpoint_to_dict(result.checkpoint)
            if result.checkpoint
            else None
        ),
        "forked_run": (
            run_to_dict(result.forked_run)
            if result.forked_run
            else None
        ),
        "metadata": dict(result.metadata),
    }


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
