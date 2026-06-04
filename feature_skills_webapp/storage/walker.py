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


@dataclass(frozen=True)
class TrackerRow:
    slug: str
    status: str  # 'in_progress' | 'available' | 'done'
    owner: str | None
    notes: str | None


_SECTION_STATUS = {
    "in-progress": "in_progress",
    "available": "available",
    "done": "done",
}
_CONTENT_CLASSES = {"feature-name", "feature-owner", "feature-notes", "feature-outcome"}


class _TrackerParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[TrackerRow] = []
        self._status: str | None = None
        self._in_tbody = False
        self._row_classes: set[str] = set()
        self._td_class: str | None = None
        self._buf: list[str] = []
        self._slug: str | None = None
        self._owner: str | None = None
        self._notes: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "section":
            sid: str = attr.get("id") or ""
            self._status = _SECTION_STATUS.get(sid)
            self._in_tbody = False
        elif tag == "tbody" and self._status is not None:
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            classes: set[str] = set((attr.get("class") or "").split())
            self._row_classes = classes
            self._slug = self._owner = self._notes = None
        elif tag == "td" and self._in_tbody:
            td_class = attr.get("class") or ""
            if td_class in _CONTENT_CLASSES:
                self._td_class = td_class
                self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "section":
            self._status = None
            self._in_tbody = False
        elif tag == "tbody":
            self._in_tbody = False
        elif tag == "tr" and self._in_tbody:
            if "empty" not in self._row_classes and self._slug and self._status:
                self.rows.append(
                    TrackerRow(
                        slug=self._slug,
                        status=self._status,
                        owner=self._owner or None,
                        notes=self._notes or None,
                    )
                )
        elif tag == "td" and self._td_class:
            text = "".join(self._buf).strip()
            if self._td_class == "feature-name":
                self._slug = text or None
            elif self._td_class == "feature-owner":
                self._owner = text or None
            elif self._td_class in ("feature-notes", "feature-outcome"):
                self._notes = text or None
            self._td_class = None
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._td_class:
            self._buf.append(data)


def parse_tracker(html: str) -> list[TrackerRow]:
    """Parse a features.html tracker into TrackerRow list. Returns [] on unrecognised shape.

    Tolerance is structural: a tracker whose markup we don't recognise simply yields
    no rows (the section-id / td-class keying matches nothing). The try/except is a
    defensive backstop — HTMLParser.feed doesn't raise on malformed markup in the
    default non-strict mode, so it rarely fires.
    """
    parser = _TrackerParser()
    try:
        parser.feed(html)
    except Exception:
        log.warning("Failed to parse tracker HTML")
        return []
    return parser.rows


def parse_doc_html(html: str) -> ParsedDoc | None:
    """Parse the feature-doc-type meta tag and title from HTML text. None if no meta tag."""
    parser = _MetaParser()
    parser.feed(html)
    if not parser.doc_type:
        return None
    return ParsedDoc(doc_type=parser.doc_type, title=parser.title)


def parse_doc(path: Path) -> ParsedDoc | None:
    """Read a doc file and parse its feature-doc-type meta tag and title.

    Returns None if the file is unreadable or carries no feature-doc-type meta tag.
    """
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("Could not read %s", path)
        return None
    return parse_doc_html(html)


def _apply_tracker_rows(
    conn: sqlite3.Connection, project_id: int, rows: list[TrackerRow], now: str
) -> None:
    for row in rows:
        conn.execute(
            "INSERT INTO features (project_id, slug, status, owner, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, slug) DO UPDATE SET "
            "status=excluded.status, owner=excluded.owner, notes=excluded.notes, updated_at=excluded.updated_at",
            (project_id, row.slug, row.status, row.owner, row.notes, now, now),
        )


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

    # Read the file once here and reuse the text for both the meta parse and (for
    # features.html) the tracker parse, rather than reading it twice.
    try:
        html_content = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("Could not read %s", abs_path)
        summary.errors += 1
        return

    parsed = parse_doc_html(html_content)
    if parsed is None:
        log.debug("Skipping %s: no feature-doc-type meta tag", abs_path)
        summary.errors += 1
        return

    project_id = _upsert_project(conn, identity.project, now)
    feature_id = (
        _upsert_feature(conn, project_id, identity.feature, now) if identity.feature else None
    )

    meta = json.dumps({"title": parsed.title, "size": st.st_size})
    desired = "archived" if identity.archived else "active"
    payload = json.dumps(
        {"path": source_path, "type": parsed.doc_type, "feature": identity.feature}
    )

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
            "INSERT INTO events (document_id, event_type, payload_json, created_at) "
            "VALUES (?, 'created', ?, ?)",
            (doc_id, payload, now),
        )
        summary.created += 1
    else:
        doc_id = row["id"]
        conn.execute(
            "UPDATE documents SET project_id=?, feature_id=?, type=?, status=?, "
            "metadata_json=?, source_mtime=?, updated_at=? WHERE id=?",
            (project_id, feature_id, parsed.doc_type, desired, meta, mtime, now, doc_id),
        )
        # Precedence note: a doc returning from 'missing' is reported as 'reactivated'
        # even if it reappears under .feedback-archive/ — reactivation wins over
        # archival. Its status is still set to 'archived' (via `desired`) above.
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
            "INSERT INTO events (document_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, event_type, payload, now),
        )

    # For project-level features.html docs, parse the tracker (reusing html_content)
    # and upsert feature metadata. Guarded so a tracker mishap can't abort the walk.
    if identity.feature is None and parsed.doc_type == "features":
        try:
            _apply_tracker_rows(conn, project_id, parse_tracker(html_content), now)
        except Exception:
            log.warning("Failed to apply tracker rows from %s", abs_path)


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
                f"SELECT d.id, d.source_path, d.type, f.slug AS feature FROM documents d "  # noqa: S608
                f"LEFT JOIN features f ON d.feature_id = f.id "
                f"WHERE d.status IN ('active', 'archived') "
                f"AND d.source_path NOT IN ({placeholders})",
                list(seen_paths),
            ).fetchall()
            for row in unseen:
                conn.execute(
                    "UPDATE documents SET status='missing', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                payload = json.dumps(
                    {"path": row["source_path"], "type": row["type"], "feature": row["feature"]}
                )
                conn.execute(
                    "INSERT INTO events (document_id, event_type, payload_json, created_at) "
                    "VALUES (?, 'missing', ?, ?)",
                    (row["id"], payload, now),
                )
                summary.missing += 1

    summary.duration_ms = int((time.monotonic() - start) * 1000)
    return summary
