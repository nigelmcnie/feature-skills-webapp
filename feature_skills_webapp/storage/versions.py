"""Content versioning: record and retrieve document versions, and backfill logical keys."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from feature_skills_webapp.storage.db import transaction
from feature_skills_webapp.storage.doc_content import ParsedContent, Section, serialise

# Extracts the instance number from a feedback filename stem, e.g. "requirements-feedback-2" → 2.
_FEEDBACK_NUM_RE = re.compile(r"-feedback-(\d+)$")


def record_version(
    conn: sqlite3.Connection,
    document_id: int,
    content: ParsedContent,
    actor: str,
    now: str,
) -> int:
    """Insert a new version row. Returns the new version_num."""
    row = conn.execute(
        "SELECT COALESCE(MAX(version_num), 0) AS max_ver "
        "FROM document_versions WHERE document_id=?",
        (document_id,),
    ).fetchone()
    next_ver = row["max_ver"] + 1
    conn.execute(
        "INSERT INTO document_versions (document_id, version_num, content_json, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (document_id, next_ver, serialise(content), actor, now),
    )
    return next_ver


def current_content(conn: sqlite3.Connection, document_id: int) -> ParsedContent | None:
    """Return the latest version's ParsedContent, or None if no versions exist."""
    row = conn.execute(
        "SELECT content_json FROM document_versions "
        "WHERE document_id=? ORDER BY version_num DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row["content_json"])
    return ParsedContent(
        shape=data["shape"],
        sections=tuple(Section(key=s["key"], body=s["body"]) for s in data["sections"]),
    )


def content_at_or_before(
    conn: sqlite3.Connection, document_id: int, ts: str
) -> ParsedContent | None:
    """Latest version with created_at <= ts, decoded; None if no such version.

    Passing the empty string as ts (the never-read sentinel) returns None since
    no real ISO timestamp satisfies created_at <= ''.
    """
    row = conn.execute(
        "SELECT content_json FROM document_versions "
        "WHERE document_id=? AND created_at <= ? ORDER BY version_num DESC LIMIT 1",
        (document_id, ts),
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row["content_json"])
    return ParsedContent(
        shape=data["shape"],
        sections=tuple(Section(key=s["key"], body=s["body"]) for s in data["sections"]),
    )


def backfill_logical_keys(conn: sqlite3.Connection) -> None:
    """Idempotent startup migration.

    Assigns logical_key to all existing document rows, resolves duplicates,
    drops the old source_path unique index, and creates the logical_key unique index.
    Called once at startup after migrate(); safe to call on every restart.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_documents_logical_key_unique'"
    ).fetchone():
        return

    with transaction(conn):
        _assign_logical_keys(conn)
        _resolve_collisions(conn)
        _swap_indexes(conn)


def _assign_logical_keys(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT d.id, d.type, d.source_path, p.name AS project, f.slug AS feature "
        "FROM documents d "
        "JOIN projects p ON d.project_id = p.id "
        "LEFT JOIN features f ON d.feature_id = f.id "
        "WHERE d.logical_key IS NULL"
    ).fetchall()
    for row in rows:
        stem = Path(row["source_path"]).stem if row["source_path"] else ""
        m = _FEEDBACK_NUM_RE.search(stem)
        instance = int(m.group(1)) if m else 1
        feature = row["feature"]
        key = f"{row['project']}/{feature or '-'}/{row['type']}/{instance}"
        conn.execute(
            "UPDATE documents SET logical_key=?, instance=? WHERE id=?",
            (key, instance, row["id"]),
        )


def _resolve_collisions(conn: sqlite3.Connection) -> None:
    _STATUS_RANK: dict[str, int] = {"active": 2, "archived": 1, "missing": 0}
    collision_keys = conn.execute(
        "SELECT logical_key FROM documents "
        "WHERE logical_key IS NOT NULL "
        "GROUP BY logical_key HAVING COUNT(*) > 1"
    ).fetchall()
    for g in collision_keys:
        key = g["logical_key"]
        rows = conn.execute(
            "SELECT id, status FROM documents WHERE logical_key=?", (key,)
        ).fetchall()
        sorted_rows = sorted(
            rows,
            key=lambda r: (_STATUS_RANK.get(r["status"], 0), r["id"]),
            reverse=True,
        )
        survivor_id = sorted_rows[0]["id"]
        loser_ids = [r["id"] for r in sorted_rows[1:]]
        for loser_id in loser_ids:
            _merge_read_state(conn, survivor_id, loser_id)
            _merge_synthesis_responses(conn, survivor_id, loser_id)
            conn.execute(
                "UPDATE comments SET document_id=? WHERE document_id=?",
                (survivor_id, loser_id),
            )
        placeholders = ",".join("?" * len(loser_ids))
        conn.execute(
            f"DELETE FROM documents WHERE id IN ({placeholders})",  # noqa: S608
            loser_ids,
        )


def _merge_read_state(conn: sqlite3.Connection, survivor_id: int, loser_id: int) -> None:
    row = conn.execute(
        "SELECT last_read_at FROM read_state WHERE document_id=?", (loser_id,)
    ).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?) "
        "ON CONFLICT(document_id) DO UPDATE SET "
        "last_read_at=MAX(last_read_at, excluded.last_read_at)",
        (survivor_id, row["last_read_at"]),
    )


def _merge_synthesis_responses(conn: sqlite3.Connection, survivor_id: int, loser_id: int) -> None:
    rows = conn.execute(
        "SELECT item_num, response, routine_flag, updated_at "
        "FROM synthesis_responses WHERE document_id=?",
        (loser_id,),
    ).fetchall()
    for s in rows:
        conn.execute(
            "INSERT INTO synthesis_responses "
            "(document_id, item_num, response, routine_flag, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(document_id, item_num) DO NOTHING",
            (survivor_id, s["item_num"], s["response"], s["routine_flag"], s["updated_at"]),
        )


def _swap_indexes(conn: sqlite3.Connection) -> None:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_documents_source_path'"
    ).fetchone():
        conn.execute("DROP INDEX idx_documents_source_path")
    conn.execute("CREATE UNIQUE INDEX idx_documents_logical_key_unique ON documents(logical_key)")
