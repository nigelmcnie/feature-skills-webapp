"""Unit tests for storage/read_state.py."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from feature_skills_webapp.storage.db import connect, migrate, now_iso
from feature_skills_webapp.storage.read_state import mark_all_read, mark_read, unread_document_ids


def temp_conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    return conn


def _seed(conn: sqlite3.Connection) -> dict[str, int]:
    """Seed two projects with one active doc each (plus archived/missing variants)."""
    now = "2020-06-01T00:00:00+00:00"
    conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (now,))
    proj_a = conn.execute("SELECT id FROM projects WHERE name='proj-a'").fetchone()["id"]

    conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-b', ?)", (now,))
    proj_b = conn.execute("SELECT id FROM projects WHERE name='proj-b'").fetchone()["id"]

    def _insert_doc(project_id: int, path: str, status: str) -> int:
        conn.execute(
            "INSERT INTO documents (project_id, type, status, source_path, metadata_json, "
            "source_mtime, created_at, updated_at) VALUES (?, 'context', ?, ?, '{}', ?, ?, ?)",
            (project_id, status, path, now, now, now),
        )
        return conn.execute("SELECT id FROM documents WHERE source_path=?", (path,)).fetchone()[
            "id"
        ]

    doc_a = _insert_doc(proj_a, "/docs/proj-a/feat/context.html", "active")
    doc_b = _insert_doc(proj_b, "/docs/proj-b/feat/context.html", "active")
    doc_archived = _insert_doc(
        proj_a, "/docs/proj-a/feat/.feedback-archive/context.html", "archived"
    )
    doc_missing = _insert_doc(proj_a, "/docs/proj-a/gone/context.html", "missing")

    OLD = "2020-01-01T00:00:00+00:00"
    FUTURE = "2099-01-01T00:00:00+00:00"

    for doc_id, ts in [
        (doc_a, OLD),
        (doc_b, OLD),
        (doc_archived, FUTURE),
        (doc_missing, FUTURE),
    ]:
        conn.execute(
            "INSERT INTO events (document_id, event_type, payload_json, created_at) "
            "VALUES (?, 'created', '{}', ?)",
            (doc_id, ts),
        )

    return {
        "proj_a": proj_a,
        "proj_b": proj_b,
        "doc_a": doc_a,
        "doc_b": doc_b,
        "doc_archived": doc_archived,
        "doc_missing": doc_missing,
    }


# --- now_iso tests ---


def test_now_iso_is_utc(tmp_path: Path) -> None:
    ts = now_iso()
    dt = datetime.fromisoformat(ts)
    offset = dt.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_now_iso_non_decreasing() -> None:
    t1 = now_iso()
    t2 = now_iso()
    assert t2 >= t1


# --- unread_document_ids tests ---


def test_no_read_state_row_is_unread(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    unread = unread_document_ids(conn)
    assert ids["doc_a"] in unread
    assert ids["doc_b"] in unread


def test_after_mark_read_no_longer_unread(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    mark_read(conn, ids["doc_a"])
    unread = unread_document_ids(conn)
    assert ids["doc_a"] not in unread
    assert ids["doc_b"] in unread


def test_later_event_reflags_as_unread(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    mark_read(conn, ids["doc_a"])
    assert ids["doc_a"] not in unread_document_ids(conn)

    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (?, 'updated', '{}', '2099-01-01T00:00:00+00:00')",
        (ids["doc_a"],),
    )
    assert ids["doc_a"] in unread_document_ids(conn)


def test_mark_read_idempotent(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    mark_read(conn, ids["doc_a"])
    mark_read(conn, ids["doc_a"])
    row_count = conn.execute(
        "SELECT COUNT(*) AS n FROM read_state WHERE document_id=?", (ids["doc_a"],)
    ).fetchone()["n"]
    assert row_count == 1
    assert ids["doc_a"] not in unread_document_ids(conn)


def test_archived_and_missing_excluded(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    unread = unread_document_ids(conn)
    assert ids["doc_archived"] not in unread
    assert ids["doc_missing"] not in unread


def test_project_filter_returns_only_target(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    unread_a = unread_document_ids(conn, project_id=ids["proj_a"])
    unread_b = unread_document_ids(conn, project_id=ids["proj_b"])
    unread_all = unread_document_ids(conn)

    assert ids["doc_a"] in unread_a
    assert ids["doc_b"] not in unread_a

    assert ids["doc_b"] in unread_b
    assert ids["doc_a"] not in unread_b

    assert ids["doc_a"] in unread_all
    assert ids["doc_b"] in unread_all


# --- mark_all_read tests ---


def test_mark_all_read_stamps_active_docs_and_returns_count(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    count = mark_all_read(conn, ids["proj_a"])
    assert count == 1  # only doc_a is active in proj_a
    assert ids["doc_a"] not in unread_document_ids(conn)


def test_mark_all_read_ignores_other_projects(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    mark_all_read(conn, ids["proj_a"])
    assert ids["doc_b"] in unread_document_ids(conn)


def test_mark_all_read_ignores_archived_and_missing(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    mark_all_read(conn, ids["proj_a"])
    # archived and missing docs should not have read_state rows written
    archived_row = conn.execute(
        "SELECT last_read_at FROM read_state WHERE document_id=?", (ids["doc_archived"],)
    ).fetchone()
    missing_row = conn.execute(
        "SELECT last_read_at FROM read_state WHERE document_id=?", (ids["doc_missing"],)
    ).fetchone()
    assert archived_row is None
    assert missing_row is None
