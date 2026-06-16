#!/usr/bin/env python3
"""Docker entrypoint for ZLAgent.

Responsibilities:
  * Ensure persistent directories exist.
  * Seed workspace with defaults without overwriting user data.
  * Start the uvicorn server bound to ZLAGENT_HOST:ZLAGENT_PORT.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR = Path("/app")
DEFAULT_WORKSPACE = APP_DIR / "workspace"
SEED_WORKSPACE = APP_DIR / "workspace_seed"


def copy_missing_tree(src: Path, dst: Path) -> int:
    """Merge ``src`` into ``dst`` without overwriting existing files.

    Recurses into directories that already exist in the destination so that
    newly-shipped seed content (e.g. v0.7's ``skills/arxiv/``) lands in a
    workspace that was created by an older image and already has
    ``skills/example-ping/``.
    """
    copied = 0
    if not src.exists():
        return copied
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            if target.exists() and target.is_dir():
                copied += copy_missing_tree(item, target)
                continue
            if target.exists():
                continue  # destination is a file shadowing a seed dir; leave alone
            shutil.copytree(item, target)
            copied += 1
        else:
            if target.exists():
                continue
            shutil.copy2(item, target)
            copied += 1
    return copied


def main() -> int:
    os.environ.setdefault("ZLAGENT_HOST", "0.0.0.0")
    os.environ.setdefault("ZLAGENT_PORT", "8020")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    (APP_DIR / "data").mkdir(parents=True, exist_ok=True)
    (APP_DIR / "config").mkdir(parents=True, exist_ok=True)
    DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    copy_missing_tree(SEED_WORKSPACE, DEFAULT_WORKSPACE)

    command = sys.argv[1:] or [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.app:app",
        "--host",
        os.environ["ZLAGENT_HOST"],
        "--port",
        os.environ["ZLAGENT_PORT"],
    ]
    return subprocess.call(command, cwd=str(APP_DIR))


if __name__ == "__main__":
    raise SystemExit(main())
