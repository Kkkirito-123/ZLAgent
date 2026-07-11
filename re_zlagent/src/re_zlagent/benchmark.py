"""Machine-readable release benchmark command."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from re_zlagent.harness.evals import (
    ReleaseBenchmarkRunner,
    load_benchmark_corpus,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="re-zlagent-benchmark")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Optional path to a compatible versioned benchmark corpus.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the JSON report.",
    )
    return parser


def run_benchmark_cli(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
) -> int:
    """Run release benchmarks and return a process-style exit code."""

    out = stdout or sys.stdout
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        corpus = load_benchmark_corpus(args.corpus)
        report = asyncio.run(ReleaseBenchmarkRunner(corpus).run())
        payload = report.to_dict()
        code = 0 if report.ok else 1
    except (OSError, RuntimeError, ValueError) as exc:
        payload = {
            "ok": False,
            "error": {
                "type": "benchmark_configuration_error",
                "message": str(exc),
            },
        }
        code = 2
    _write_json(payload, stdout=out, pretty=args.pretty)
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
    raise SystemExit(run_benchmark_cli())


if __name__ == "__main__":
    main()
