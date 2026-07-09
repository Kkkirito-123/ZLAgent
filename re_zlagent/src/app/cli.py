"""JSON CLI surface for re_zlagent operator controls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

from harness.storage import SqliteTaskStore

from .operator import OperatorResponse, OperatorService


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
        operator = OperatorService(store)
        if command == "status":
            response = operator.status(args.run_id)
        elif command == "pause":
            response = operator.pause(
                args.run_id,
                reason=args.reason,
                actor=args.actor,
            )
        elif command == "resume":
            response = operator.resume(
                args.run_id,
                feedback=args.feedback,
                actor=args.actor,
            )
        elif command == "cancel":
            response = operator.cancel(
                args.run_id,
                reason=args.reason,
                actor=args.actor,
            )
        elif command == "fork":
            response = operator.fork(
                args.run_id,
                new_run_id=args.new_run_id,
                reason=args.reason,
                actor=args.actor,
                checkpoint_id=args.checkpoint_id,
            )
        else:
            raise CliArgumentError(f"unknown command: {command}")

        _write_json(response.to_dict(), stdout=stdout, pretty=pretty)
        return _exit_code(response)
    finally:
        store.close()


def _exit_code(response: OperatorResponse) -> int:
    if response.ok:
        return 0
    if response.command == "status":
        return 1
    return 2


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
