"""Chinese project-effectiveness benchmark command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from re_zlagent.harness.agent import IntentRouter, JsonIntentRouter
from re_zlagent.harness.evals import (
    EffectivenessBenchmarkRunner,
    EffectivenessTrack,
    ProjectEffectivenessExecutors,
    load_effectiveness_corpus,
)
from re_zlagent.harness.model import ModelClient, OpenAICompatibleModelConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="re-zlagent-effectiveness")
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument(
        "--track",
        action="append",
        choices=[track.value for track in EffectivenessTrack],
        help="Run only one track; repeat this option to select several tracks.",
    )
    parser.add_argument(
        "--deterministic-only",
        action="store_true",
        help="Skip provider-backed tracks and run only local deterministic tracks.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the manifest and JSONL cases without executing them.",
    )
    parser.add_argument(
        "--output-format",
        choices=("json", "jsonl", "markdown"),
        default="json",
    )
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--api-key-env", default=None)
    auth.add_argument("--no-api-key", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=60)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    return parser


def run_effectiveness_cli(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    intent_router: IntentRouter | None = None,
    task_model: ModelClient | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Validate or execute the Chinese effectiveness corpus."""

    out = stdout or sys.stdout
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        corpus = load_effectiveness_corpus(args.manifest, args.cases)
        tracks = _selected_tracks(args)
        if args.validate_only:
            payload = _validation_payload(corpus, tracks)
            _write_json(payload, stdout=out, pretty=args.pretty)
            return 0
        resolved_router = intent_router
        resolved_task_model = task_model
        needs_provider_model = (
            EffectivenessTrack.AGENT_TASK in tracks and resolved_task_model is None
        ) or (EffectivenessTrack.INTENT_ROUTING in tracks and resolved_router is None)
        provider_model = (
            _model_from_args(args, environ=environ) if needs_provider_model else None
        )
        if EffectivenessTrack.INTENT_ROUTING in tracks and resolved_router is None:
            assert provider_model is not None
            resolved_router = JsonIntentRouter(provider_model)
        if EffectivenessTrack.AGENT_TASK in tracks and resolved_task_model is None:
            assert provider_model is not None
            resolved_task_model = provider_model
        report = asyncio.run(
            EffectivenessBenchmarkRunner(
                corpus,
                executors=ProjectEffectivenessExecutors(
                    intent_router=resolved_router,
                    task_model=resolved_task_model,
                ).as_mapping(),
            ).run(tracks=tracks)
        )
        if args.output_format == "jsonl":
            out.write(report.to_jsonl())
        elif args.output_format == "markdown":
            out.write(report.to_markdown())
        else:
            _write_json(report.to_dict(), stdout=out, pretty=args.pretty)
        return 0 if report.ok else 1
    except (OSError, RuntimeError, ValueError) as exc:
        _write_json(
            {
                "ok": False,
                "error": {
                    "type": "effectiveness_configuration_error",
                    "message": str(exc),
                },
            },
            stdout=out,
            pretty=False,
        )
        return 2


def _selected_tracks(args: argparse.Namespace) -> tuple[EffectivenessTrack, ...]:
    if args.track:
        tracks = tuple(EffectivenessTrack(value) for value in args.track)
    elif args.deterministic_only:
        tracks = tuple(
            track
            for track in EffectivenessTrack
            if track
            not in {
                EffectivenessTrack.INTENT_ROUTING,
                EffectivenessTrack.AGENT_TASK,
            }
        )
    else:
        tracks = tuple(EffectivenessTrack)
    provider_tracks = {
        EffectivenessTrack.INTENT_ROUTING,
        EffectivenessTrack.AGENT_TASK,
    }
    if args.deterministic_only and provider_tracks.intersection(tracks):
        raise ValueError(
            "--deterministic-only cannot be combined with provider-backed tracks"
        )
    if len(tracks) != len(set(tracks)):
        raise ValueError("duplicate --track values are not allowed")
    return tracks


def _validation_payload(
    corpus: Any, tracks: tuple[EffectivenessTrack, ...]
) -> dict[str, Any]:
    cases = [case for case in corpus.cases if case.track in set(tracks)]
    capability_coverage = {
        capability: sum(capability in case.capabilities for case in cases)
        for capability in corpus.manifest.required_capabilities
    }
    return {
        "ok": True,
        "validated_only": True,
        "schema_version": corpus.manifest.schema_version,
        "corpus_version": corpus.manifest.corpus_version,
        "corpus_status": corpus.manifest.status,
        "language": corpus.manifest.language,
        "aggregate": {
            "cases": len(cases),
            "observations": sum(case.repetitions for case in cases),
        },
        "required_capabilities": list(corpus.manifest.required_capabilities),
        "capability_coverage": capability_coverage,
        "by_track": {
            track.value: {
                "cases": sum(case.track is track for case in cases),
                "observations": sum(
                    case.repetitions for case in cases if case.track is track
                ),
            }
            for track in tracks
        },
    }


def _model_from_args(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None,
) -> Any:
    values = os.environ if environ is None else environ
    base_url = args.base_url or values.get("ZLAGENT_MODEL_BASE_URL")
    model_name = args.model or values.get("ZLAGENT_MODEL_NAME")
    api_key_env = (
        args.api_key_env or values.get("ZLAGENT_API_KEY_ENV") or "OPENAI_API_KEY"
    )
    max_tokens = args.max_output_tokens
    if max_tokens is None and values.get("ZLAGENT_MODEL_MAX_TOKENS"):
        try:
            max_tokens = int(values["ZLAGENT_MODEL_MAX_TOKENS"])
        except ValueError as exc:
            raise ValueError("ZLAGENT_MODEL_MAX_TOKENS must be an integer") from exc
    if not base_url:
        raise ValueError("--base-url or ZLAGENT_MODEL_BASE_URL is required")
    if not model_name:
        raise ValueError("--model or ZLAGENT_MODEL_NAME is required")
    return OpenAICompatibleModelConfig(
        base_url=base_url,
        model=model_name,
        api_key_env=None if args.no_api_key else api_key_env,
        timeout_seconds=args.timeout_seconds,
        temperature=0,
        max_tokens=max_tokens,
    ).build_client(environ=values)


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
    raise SystemExit(run_effectiveness_cli())


if __name__ == "__main__":
    main()
