"""ZLAgent — Build a standalone HTML map of cities in the wiki.

Produces ``data/visited_map.html`` (or a path of your choice). The
file is fully self-contained: double-click to open in a browser,
no server needed. Implements v0.39.4 of ZLAgent's roadmap.

Usage::

    # Default: writes data/visited_map.html
    python scripts/build_visited_map.py

    # Custom output path
    python scripts/build_visited_map.py --out workspace/my_map.html

    # Only show facts from the last 30 days
    python scripts/build_visited_map.py --since-days 30

    # Lower the "red" threshold (default 10) to see edges heat up faster
    python scripts/build_visited_map.py --red-threshold 3

Exits 0 on success, 1 on any unexpected error.
"""
from __future__ import annotations

import argparse
import io
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
# Force UTF-8 stdout so Chinese log lines render on Windows consoles.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# Align env with smoke/demo so we read the same SQLite file the
# running service writes to. No .env so the script works in CI.
os.environ.setdefault("ZLAGENT_DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("ZLAGENT_CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("ZLAGENT_WORKSPACE_DIR", str(ROOT / "workspace"))
os.environ.setdefault(
    "ZLAGENT_DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'zlagent.db'}",
)
from backend.core.config import Settings as _S  # noqa: E402

_S.model_config["env_file"] = None

from backend.db.models import init_engine, session_scope  # noqa: E402
from backend.wiki.geo_store import GeoStore  # noqa: E402
from backend.wiki.store import WikiStore  # noqa: E402
from backend.domains.travel.visited_map import VisitedMapBuilder  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build a standalone echarts HTML map of cities "
                    "the wiki has atomic facts for.",
    )
    p.add_argument(
        "--out", "-o",
        default=str(ROOT / "data" / "visited_map.html"),
        help="Output HTML file path (default: data/visited_map.html)",
    )
    p.add_argument(
        "--since-days", type=int, default=None,
        help="Only include facts created within the last N days. "
             "Default: all time.",
    )
    p.add_argument(
        "--red-threshold", type=int, default=10,
        help="Edge hit count above which the edge + its endpoints turn red. "
             "Default: 10.",
    )
    p.add_argument(
        "--max-entries", type=int, default=2000,
        help="Cap on wiki rows pulled for the build. Default: 2000.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    # The engine is normally created by app.lifespan; for a CLI one-shot
    # we spin it up manually against the same DB path.
    init_engine()

    wiki_store = WikiStore()
    geo_store = GeoStore(session_scope)

    # Make sure geo seed data is loaded. Idempotent: skips existing
    # rows. Harmless for a freshly-seeded DB, essential for a fresh
    # checkout where the service was never started.
    seed_report = geo_store.seed_from_json()
    print(f"[geo] seed: {seed_report}")

    builder = VisitedMapBuilder(
        wiki_store,
        geo_store,
        red_threshold=args.red_threshold,
        max_entries=args.max_entries,
    )

    out_path = pathlib.Path(args.out).resolve()
    try:
        written = builder.write_html(out_path, since_days=args.since_days)
    except Exception as exc:  # noqa: BLE001
        print(f"[visited-map] build failed: {exc}", file=sys.stderr)
        return 1

    # Report a small summary so ops can sanity-check without opening
    # the file.
    data = builder.build(since_days=args.since_days)
    meta = data["meta"]
    print(
        f"[visited-map] wrote {written}\n"
        f"               cities={meta['total_cities']} "
        f"edges={meta['total_edges']} facts={meta['total_facts']}\n"
        f"               red_threshold={meta['red_threshold']} "
        f"since_days={meta['since_days']}\n"
        f"               blacklist={meta['blacklist']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
