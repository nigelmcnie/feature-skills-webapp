"""Standalone import CLI: feature-skills-import --db PATH --docs-root DIR [--reconcile]."""

from __future__ import annotations

import argparse
from pathlib import Path

from feature_skills_webapp.storage.db import open_db
from feature_skills_webapp.storage.walker import walk


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="feature-skills-import",
        description="Import feature-skills HTML documents into a webapp DB.",
    )
    p.add_argument("--db", required=True, type=Path, metavar="PATH", help="SQLite database path")
    p.add_argument(
        "--docs-root",
        required=True,
        type=Path,
        metavar="DIR",
        help="Root directory of the dev-store HTML files",
    )
    p.add_argument(
        "--reconcile",
        action="store_true",
        default=False,
        help="Mark documents whose source file has been removed as missing",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Standalone importer: feature-skills-import --db PATH --docs-root DIR [--reconcile]."""
    args = _parse_args(argv)
    with open_db(args.db) as conn:
        summary = walk(conn, args.docs_root, reconcile=args.reconcile)
    print(summary)
    return 0
