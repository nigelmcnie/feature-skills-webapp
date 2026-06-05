"""Read-state storage operations: mark_read, unread_document_ids."""

from __future__ import annotations

import sqlite3

from feature_skills_webapp.storage.db import now_iso, transaction


def mark_read(conn: sqlite3.Connection, document_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?) "
            "ON CONFLICT(document_id) DO UPDATE SET last_read_at = excluded.last_read_at",
            (document_id, now_iso()),
        )


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
