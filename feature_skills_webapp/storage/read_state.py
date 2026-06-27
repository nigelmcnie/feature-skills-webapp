"""Read-state storage operations: mark_read, mark_all_read, unread_document_ids."""

from __future__ import annotations

import sqlite3

from feature_skills_webapp.storage.db import now_iso, transaction

# SQL fragment expressing "this doc has at least one version the user hasn't acknowledged".
# Requires the documents table to be aliased as `d` in the enclosing query.
UNREVIEWED_CHANGES_SQL = (
    "(SELECT MAX(version_num) FROM document_versions WHERE document_id = d.id)"
    " > COALESCE((SELECT acked_version FROM read_state WHERE document_id = d.id), 0)"
)


def mark_read(conn: sqlite3.Connection, document_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?) "
            "ON CONFLICT(document_id) DO UPDATE SET last_read_at = excluded.last_read_at",
            (document_id, now_iso()),
        )


def mark_all_read(conn: sqlite3.Connection, project_id: int) -> int:
    now = now_iso()
    with transaction(conn):
        rows = conn.execute(
            "SELECT id FROM documents WHERE project_id = ? AND status = 'active'",
            (project_id,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO read_state (document_id, last_read_at, acked_version) "
                "VALUES (?, ?, (SELECT COALESCE(MAX(version_num), 0) FROM document_versions WHERE document_id = ?)) "
                "ON CONFLICT(document_id) DO UPDATE SET "
                "last_read_at = excluded.last_read_at, "
                "acked_version = excluded.acked_version",
                (r["id"], now, r["id"]),
            )
    return len(rows)


def mark_documents_read(conn: sqlite3.Connection, document_ids: list[int]) -> int:
    if not document_ids:
        return 0
    now = now_iso()
    with transaction(conn):
        for doc_id in document_ids:
            conn.execute(
                "INSERT INTO read_state (document_id, last_read_at, acked_version) "
                "VALUES (?, ?, (SELECT COALESCE(MAX(version_num), 0) FROM document_versions WHERE document_id = ?)) "
                "ON CONFLICT(document_id) DO UPDATE SET "
                "last_read_at = excluded.last_read_at, "
                "acked_version = excluded.acked_version",
                (doc_id, now, doc_id),
            )
    return len(document_ids)


def last_read_at(conn: sqlite3.Connection, document_id: int) -> str | None:
    """The doc's last_read_at, or None if never read."""
    row = conn.execute(
        "SELECT last_read_at FROM read_state WHERE document_id = ?", (document_id,)
    ).fetchone()
    return row["last_read_at"] if row is not None else None


def acked_version(conn: sqlite3.Connection, document_id: int) -> int | None:
    """The version_num last acknowledged via the diff view; None if never acknowledged."""
    row = conn.execute(
        "SELECT acked_version FROM read_state WHERE document_id = ?", (document_id,)
    ).fetchone()
    return row["acked_version"] if row is not None else None


def mark_version_seen(conn: sqlite3.Connection, document_id: int) -> None:
    """Acknowledge the current version: advance acked_version to MAX(version_num).

    Called whenever a doc's current content has been seen — reviewing a section
    doc's diff, or simply viewing a doc (e.g. synthesis docs, which have no
    separate diff-review step). Idempotent.
    """
    with transaction(conn):
        conn.execute(
            "INSERT INTO read_state (document_id, last_read_at, acked_version) "
            "VALUES (?, ?, ("
            "  SELECT COALESCE(MAX(version_num), 0) FROM document_versions WHERE document_id = ?"
            ")) ON CONFLICT(document_id) DO UPDATE SET "
            "acked_version = excluded.acked_version",
            (document_id, now_iso(), document_id),
        )


def has_unreviewed_changes(conn: sqlite3.Connection, document_id: int) -> bool:
    """True when the latest version exceeds the acknowledged version (or was never acked)."""
    row = conn.execute(
        "SELECT (SELECT MAX(version_num) FROM document_versions WHERE document_id = ?)"
        " > COALESCE((SELECT acked_version FROM read_state WHERE document_id = ?), 0)"
        " AS unreviewed",
        (document_id, document_id),
    ).fetchone()
    return bool(row["unreviewed"]) if row is not None else False


def unread_document_ids(conn: sqlite3.Connection, project_id: int | None = None) -> list[int]:
    sql = (
        "SELECT d.id FROM documents d "
        "WHERE d.status = 'active' "
        "AND EXISTS ("
        "  SELECT 1 FROM events e "
        "  WHERE e.document_id = d.id "
        "  AND e.created_at > COALESCE("
        "    (SELECT last_read_at FROM read_state WHERE document_id = d.id), ''"
        "  )"
        ")"
    )
    params: list[object] = []
    if project_id is not None:
        sql += " AND d.project_id = ?"  # noqa: S608
        params.append(project_id)
    return [row["id"] for row in conn.execute(sql, params).fetchall()]
