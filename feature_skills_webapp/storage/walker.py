"""Walker: indexes the dev-store into the SQLite schema."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from feature_skills_webapp.storage.db import now_iso, transaction
from feature_skills_webapp.storage.doc_content import manifest_for, parse_content, serialise
from feature_skills_webapp.storage.versions import current_content, record_version

log = logging.getLogger(__name__)

FEEDBACK_SUFFIX = "-feedback"
_FEEDBACK_RE = re.compile(r"^(?P<phase>[a-z]+)-feedback-(?P<num>\d+)$")


def feedback_type(rel_path: Path) -> str | None:
    """Synthetic doc type for a feedback doc, e.g. 'requirements-feedback'. None if not one."""
    m = _FEEDBACK_RE.match(rel_path.stem)
    return f"{m.group('phase')}{FEEDBACK_SUFFIX}" if m else None


def feedback_instance(rel_path: Path) -> int:
    """Return the instance N from a feedback filename (e.g. 2 from requirements-feedback-2.html)."""
    m = _FEEDBACK_RE.match(rel_path.stem)
    return int(m.group("num")) if m else 1


def logical_key(project: str, feature: str | None, doc_type: str, instance: int) -> str:
    """Canonical stable identity key for a document: '{project}/{feature or '-'}/{doc_type}/{instance}'."""
    return f"{project}/{feature or '-'}/{doc_type}/{instance}"


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
    shipped: int = 0
    errors: int = 0
    unparsed: int = 0
    duration_ms: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.created
            or self.updated
            or self.archived
            or self.missing
            or self.reactivated
            or self.shipped
        )


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
    conn: sqlite3.Connection,
    project_id: int,
    project_name: str,
    rows: list[TrackerRow],
    now: str,
    summary: WalkSummary,
) -> None:
    for row in rows:
        prev = conn.execute(
            "SELECT status FROM features WHERE project_id=? AND slug=?",
            (project_id, row.slug),
        ).fetchone()
        old_status = prev["status"] if prev else None
        conn.execute(
            "INSERT INTO features (project_id, slug, status, owner, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, slug) DO UPDATE SET "
            "status=excluded.status, owner=excluded.owner, notes=excluded.notes, updated_at=excluded.updated_at",
            (project_id, row.slug, row.status, row.owner, row.notes, now, now),
        )
        if row.status == "done" and old_status != "done":
            conn.execute(
                "INSERT INTO events (document_id, event_type, payload_json, created_at) "
                "VALUES (NULL, 'shipped', ?, ?)",
                (json.dumps({"project": project_name, "slug": row.slug}), now),
            )
            summary.shipped += 1


def upsert_project(conn: sqlite3.Connection, name: str, now: str) -> int:
    conn.execute(
        "INSERT INTO projects (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
        (name, now),
    )
    return conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()["id"]


def upsert_feature(conn: sqlite3.Connection, project_id: int, slug: str, now: str) -> int:
    conn.execute(
        "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
        "VALUES (?, ?, 'available', ?, ?) "
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
    mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()

    # Derive doc_type hint and instance from path for the logical_key lookup.
    # The meta tag (read below) confirms the stored type; path derivation is consistent
    # for all well-formed docs and avoids reading the file just to do the lookup.
    ft = feedback_type(rel_path)
    doc_type_hint = ft if ft is not None else rel_path.stem
    instance = feedback_instance(rel_path) if ft is not None else 1
    lkey = logical_key(identity.project, identity.feature, doc_type_hint, instance)

    row = conn.execute(
        "SELECT id, status, source_path, source_mtime, metadata_json "
        "FROM documents WHERE logical_key=?",
        (lkey,),
    ).fetchone()

    cached_size = json.loads(row["metadata_json"] or "{}").get("size") if row else None

    # Gate: skip file read when status is not 'missing', mtime+size unchanged, and a
    # version already exists. Still update source_path/status if the file moved.
    cur = None
    if row:
        cur = current_content(conn, row["id"])
        need_read = (
            row["status"] == "missing"
            or row["source_mtime"] != mtime
            or cached_size != st.st_size
            or cur is None
        )
        if not need_read:
            desired = "archived" if identity.archived else "active"
            if row["source_path"] != source_path or row["status"] != desired:
                conn.execute(
                    "UPDATE documents SET source_path=?, status=?, updated_at=? WHERE id=?",
                    (source_path, desired, now, row["id"]),
                )
            return

    # Read the file once here and reuse the text for both the meta parse and (for
    # features.html) the tracker parse, rather than reading it twice.
    try:
        html_content = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("Could not read %s", abs_path)
        summary.errors += 1
        return

    mp = _MetaParser()
    mp.feed(html_content)
    doc_type = mp.doc_type or ft
    if doc_type is None:
        log.debug("Skipping %s: no meta tag and not a feedback doc", abs_path)
        summary.errors += 1
        return
    parsed = ParsedDoc(doc_type=doc_type, title=mp.title)

    # Parse structured content for versioning.
    content = parse_content(html_content, manifest_for(doc_type))
    if content.shape == "sections" and not content.sections:
        log.warning("Section-parse failure (no main/zero sections): %s", abs_path)
        summary.unparsed += 1

    project_id = upsert_project(conn, identity.project, now)
    feature_id = (
        upsert_feature(conn, project_id, identity.feature, now) if identity.feature else None
    )

    meta = json.dumps({"title": parsed.title, "size": st.st_size})
    desired = "archived" if identity.archived else "active"
    payload = json.dumps(
        {"path": source_path, "type": parsed.doc_type, "feature": identity.feature}
    )

    if row is None:
        cursor = conn.execute(
            "INSERT INTO documents "
            "(project_id, feature_id, type, status, source_path, logical_key, instance, "
            "metadata_json, source_mtime, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                feature_id,
                parsed.doc_type,
                desired,
                source_path,
                lkey,
                instance,
                meta,
                mtime,
                now,
                now,
            ),
        )
        doc_id = cursor.lastrowid
        assert doc_id is not None
        record_version(conn, doc_id, content, actor="importer", now=now)
        conn.execute(
            "INSERT INTO events (document_id, event_type, payload_json, created_at) "
            "VALUES (?, 'created', ?, ?)",
            (doc_id, payload, now),
        )
        summary.created += 1
    else:
        doc_id = row["id"]
        old_status = row["status"]
        conn.execute(
            "UPDATE documents SET project_id=?, feature_id=?, type=?, status=?, "
            "source_path=?, logical_key=?, instance=?, metadata_json=?, source_mtime=?, "
            "updated_at=? WHERE id=?",
            (
                project_id,
                feature_id,
                parsed.doc_type,
                desired,
                source_path,
                lkey,
                instance,
                meta,
                mtime,
                now,
                doc_id,
            ),
        )

        if cur is None:
            # Seed the first version silently — no event, no summary counter.
            record_version(conn, doc_id, content, actor="importer", now=now)
        elif serialise(cur) != serialise(content):
            # Content changed: record the new version and emit the appropriate event.
            # Precedence: reactivation wins over archival (a doc returning from 'missing'
            # is reported as 'reactivated' even if it reappears under .feedback-archive/).
            record_version(conn, doc_id, content, actor="importer", now=now)
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
        # else: content identical — metadata already updated, no version or event.

    # For project-level features.html docs, parse the tracker (reusing html_content)
    # and upsert feature metadata. Guarded so a tracker mishap can't abort the walk.
    if identity.feature is None and parsed.doc_type == "features":
        try:
            _apply_tracker_rows(
                conn, project_id, identity.project, parse_tracker(html_content), now, summary
            )
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

    now = now_iso()
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
