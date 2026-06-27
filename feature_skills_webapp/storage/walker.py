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
from feature_skills_webapp.storage.doc_content import manifest_for, parse_content
from feature_skills_webapp.storage.parents import (
    logical_key,
    slugify,
    upsert_feature,
    upsert_project,
)
from feature_skills_webapp.storage.versions import current_content

# Re-export so existing importers (tests, tracker, web) keep working unchanged.
__all__ = [
    "logical_key",
    "slugify",
    "upsert_feature",
    "upsert_project",
]

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


def _process_file(
    conn: sqlite3.Connection,
    abs_path: Path,
    rel_path: Path,
    identity: DocIdentity,
    summary: WalkSummary,
    now: str,
) -> None:
    # Imported lazily to avoid the documents↔walker import cycle: walker imports
    # submit_document from documents; documents imports logical_key from parents (not walker).
    from feature_skills_webapp.storage.documents import submit_document

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

    # Read the file once here and reuse the text for the meta parse and section
    # content, rather than reading it twice.
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

    # Parse structured content for versioning.
    content = parse_content(html_content, manifest_for(doc_type))
    if content.shape == "sections" and not content.sections:
        log.warning("Section-parse failure (no main/zero sections): %s", abs_path)
        summary.unparsed += 1

    # Declare parents from disk — walker creates on demand (upsert, not require).
    project_id = upsert_project(conn, identity.project, now)
    if identity.feature:
        upsert_feature(conn, project_id, identity.feature, now)

    desired = "archived" if identity.archived else "active"
    result = submit_document(
        conn,
        project=identity.project,
        feature=identity.feature,
        doc_type=doc_type,
        instance=instance,
        content=content,
        actor="importer",
        now=now,
        source_path=source_path,
        source_mtime=mtime,
        doc_status=desired,
        doc_size=st.st_size,
        doc_title=mp.title,
    )

    if result.event_type == "created":
        summary.created += 1
    elif result.event_type == "updated":
        summary.updated += 1
    elif result.event_type == "archived":
        summary.archived += 1
    elif result.event_type == "reactivated":
        summary.reactivated += 1
    # else: no change or silent version seed — no summary counter


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
