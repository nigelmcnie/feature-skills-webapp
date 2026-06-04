"""Walker: indexes the dev-store into the SQLite schema."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from feature_skills_webapp.storage.db import transaction

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocIdentity:
    project: str
    feature: str | None  # None for project-level docs
    archived: bool


@dataclass(frozen=True)
class ParsedDoc:
    doc_type: str
    title: str | None


@dataclass
class WalkSummary:
    created: int = 0
    updated: int = 0
    archived: int = 0
    missing: int = 0
    reactivated: int = 0
    errors: int = 0
    duration_ms: int = 0


def identity_for(rel_path: Path) -> DocIdentity | None:
    """Map a docs-root-relative .html path to its identity, or None to skip."""
    parts = rel_path.parts
    # Depth-2: (project, "features.html") — project-level doc
    if len(parts) == 2 and parts[1] == "features.html":
        return DocIdentity(project=parts[0], feature=None, archived=False)
    # Depth-3: (project, feature, "<doc>.html") — active feature doc
    if len(parts) == 3 and not parts[1].startswith("."):
        return DocIdentity(project=parts[0], feature=parts[1], archived=False)
    # Depth-4: (project, feature, ".feedback-archive", "<doc>.html") — archived
    if len(parts) == 4 and not parts[1].startswith(".") and parts[2] == ".feedback-archive":
        return DocIdentity(project=parts[0], feature=parts[1], archived=True)
    return None


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.doc_type: str | None = None
        self.title: str | None = None
        self._in_title = False
        self._title_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            attr_dict = dict(attrs)
            if attr_dict.get("name") == "feature-doc-type":
                self.doc_type = attr_dict.get("content")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            self.title = "".join(self._title_buf).strip() or None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)


def parse_doc(path: Path) -> ParsedDoc | None:
    """Parse a doc's feature-doc-type meta tag and title. Returns None if no meta tag."""
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("Could not read %s", path)
        return None
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:
        log.warning("Parse error in %s", path)
        return None
    if not parser.doc_type:
        log.debug("Skipping %s: no feature-doc-type meta tag", path)
        return None
    return ParsedDoc(doc_type=parser.doc_type, title=parser.title)


def _upsert_project(conn: sqlite3.Connection, name: str, now: str) -> int:
    conn.execute(
        "INSERT INTO projects (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
        (name, now),
    )
    return conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()["id"]


def _upsert_feature(conn: sqlite3.Connection, project_id: int, slug: str, now: str) -> int:
    conn.execute(
        "INSERT INTO features (project_id, slug, created_at, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(project_id, slug) DO NOTHING",
        (project_id, slug, now, now),
    )
    return conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug=?", (project_id, slug)
    ).fetchone()["id"]


def _process_file(
    conn: sqlite3.Connection,
    abs_path: Path,
    rel_path: Path,
    identity: DocIdentity,
    summary: WalkSummary,
    now: str,
) -> None:
    try:
        st = abs_path.stat()
    except OSError:
        log.warning("Could not stat %s", abs_path)
        summary.errors += 1
        return

    source_path = str(abs_path)
    row = conn.execute(
        "SELECT id, status, source_mtime, metadata_json FROM documents WHERE source_path=?",
        (source_path,),
    ).fetchone()

    mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()
    cached_size = json.loads(row["metadata_json"] or "{}").get("size") if row else None

    # Gate: skip read+write if unchanged
    if (
        row
        and row["status"] != "missing"
        and row["source_mtime"] == mtime
        and cached_size == st.st_size
    ):
        return

    parsed = parse_doc(abs_path)
    if parsed is None:
        summary.errors += 1
        return

    project_id = _upsert_project(conn, identity.project, now)
    feature_id = (
        _upsert_feature(conn, project_id, identity.feature, now) if identity.feature else None
    )

    meta = json.dumps({"title": parsed.title, "size": st.st_size})
    desired = "archived" if identity.archived else "active"

    if row is None:
        conn.execute(
            "INSERT INTO documents "
            "(project_id, feature_id, type, status, source_path, metadata_json, source_mtime, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, feature_id, parsed.doc_type, desired, source_path, meta, mtime, now, now),
        )
        doc_id = conn.execute(
            "SELECT id FROM documents WHERE source_path=?", (source_path,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO events (document_id, event_type, created_at) VALUES (?, 'created', ?)",
            (doc_id, now),
        )
        summary.created += 1
    else:
        doc_id = row["id"]
        conn.execute(
            "UPDATE documents SET project_id=?, feature_id=?, type=?, status=?, "
            "metadata_json=?, source_mtime=?, updated_at=? WHERE id=?",
            (project_id, feature_id, parsed.doc_type, desired, meta, mtime, now, doc_id),
        )
        old_status = row["status"]
        if old_status == "missing":
            event_type = "reactivated"
            summary.reactivated += 1
        elif desired == "archived" and old_status != "archived":
            event_type = "archived"
            summary.archived += 1
        else:
            event_type = "updated"
            summary.updated += 1
        conn.execute(
            "INSERT INTO events (document_id, event_type, created_at) VALUES (?, ?, ?)",
            (doc_id, event_type, now),
        )


def walk(conn: sqlite3.Connection, docs_root: Path, *, reconcile: bool) -> WalkSummary:
    """Index every *.html under docs_root.

    With reconcile=True, mark rows whose source_path was not seen as status='missing'.
    All writes run inside one transaction per the existing storage conventions.
    """
    start = time.monotonic()
    summary = WalkSummary()

    if not docs_root.exists():
        log.info("docs_root %s does not exist — walk is a no-op", docs_root)
        summary.duration_ms = int((time.monotonic() - start) * 1000)
        return summary

    now = datetime.now(tz=UTC).isoformat()
    seen_paths: set[str] = set()

    with transaction(conn):
        for abs_path in sorted(docs_root.rglob("*.html")):
            rel_path = abs_path.relative_to(docs_root)
            identity = identity_for(rel_path)
            if identity is None:
                log.debug("Skipping unrecognised path shape: %s", rel_path)
                continue
            seen_paths.add(str(abs_path))
            _process_file(conn, abs_path, rel_path, identity, summary, now)

        if reconcile:
            placeholders = ",".join("?" * len(seen_paths)) if seen_paths else "NULL"
            unseen = conn.execute(
                f"SELECT id, source_path FROM documents "  # noqa: S608
                f"WHERE status IN ('active', 'archived') "
                f"AND source_path NOT IN ({placeholders})",
                list(seen_paths),
            ).fetchall()
            for row in unseen:
                conn.execute(
                    "UPDATE documents SET status='missing', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                conn.execute(
                    "INSERT INTO events (document_id, event_type, created_at) VALUES (?, 'missing', ?)",
                    (row["id"], now),
                )
                summary.missing += 1

    summary.duration_ms = int((time.monotonic() - start) * 1000)
    return summary
