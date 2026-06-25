"""Unit tests for storage/read_state.py."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from feature_skills_webapp.storage.db import connect, migrate, now_iso
from feature_skills_webapp.storage.doc_content import ParsedContent, Section
from feature_skills_webapp.storage.read_state import (
    acked_version,
    has_unreviewed_changes,
    last_read_at,
    mark_all_read,
    mark_diff_seen,
    mark_documents_read,
    mark_read,
    unread_document_ids,
)


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


def test_equal_timestamp_tie_reads_as_read(tmp_path: Path) -> None:
    """An event whose created_at exactly equals last_read_at reads as already-read.

    The unread comparison is strict (>), a deliberate bias: don't re-flag a doc
    the instant after it was read. This pins that contract so a future change to
    the comparison operator can't silently regress it.
    """
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    TIE = "2050-01-01T00:00:00+00:00"
    # Stamp doc_a read at exactly TIE, then add an event at the same instant.
    conn.execute(
        "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?)",
        (ids["doc_a"], TIE),
    )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (?, 'updated', '{}', ?)",
        (ids["doc_a"], TIE),
    )
    # Equal timestamp must NOT count as unread.
    assert ids["doc_a"] not in unread_document_ids(conn)
    # Sanity: one microsecond later DOES re-flag it.
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (?, 'updated', '{}', '2050-01-01T00:00:00.000001+00:00')",
        (ids["doc_a"],),
    )
    assert ids["doc_a"] in unread_document_ids(conn)


# --- mark_documents_read tests ---


def test_mark_documents_read_stamps_ids_and_returns_count(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    count = mark_documents_read(conn, [ids["doc_a"], ids["doc_b"]])
    assert count == 2
    assert ids["doc_a"] not in unread_document_ids(conn)
    assert ids["doc_b"] not in unread_document_ids(conn)


def test_mark_documents_read_empty_list_is_noop(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    count = mark_documents_read(conn, [])
    assert count == 0
    assert ids["doc_a"] in unread_document_ids(conn)


def test_mark_documents_read_upserts(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    mark_documents_read(conn, [ids["doc_a"]])
    # second call should not raise and should leave doc_a read
    mark_documents_read(conn, [ids["doc_a"]])
    row_count = conn.execute(
        "SELECT COUNT(*) AS n FROM read_state WHERE document_id=?", (ids["doc_a"],)
    ).fetchone()["n"]
    assert row_count == 1
    assert ids["doc_a"] not in unread_document_ids(conn)


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


# --- last_read_at ---


def test_last_read_at_returns_none_for_never_read(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    assert last_read_at(conn, ids["doc_a"]) is None


def test_last_read_at_returns_stamp_after_mark_read(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    mark_read(conn, ids["doc_a"])
    result = last_read_at(conn, ids["doc_a"])
    assert result is not None
    assert result.endswith("+00:00")


def test_last_read_at_reflects_latest_after_double_mark(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    mark_read(conn, ids["doc_a"])
    first = last_read_at(conn, ids["doc_a"])
    mark_read(conn, ids["doc_a"])
    second = last_read_at(conn, ids["doc_a"])
    assert first is not None
    assert second is not None
    assert second >= first


# --- acked_version ---


def test_acked_version_returns_none_for_no_row(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    assert acked_version(conn, ids["doc_a"]) is None


def test_acked_version_returns_set_value(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    conn.execute(
        "INSERT INTO read_state (document_id, last_read_at, acked_version) VALUES (?, ?, 3)",
        (ids["doc_a"], "2026-01-01T00:00:00+00:00"),
    )
    assert acked_version(conn, ids["doc_a"]) == 3


# --- mark_diff_seen ---


def _make_content() -> ParsedContent:
    return ParsedContent(shape="sections", sections=(Section(key="overview", body="<p>x</p>"),))


def test_mark_diff_seen_inserts_row_with_max_version(tmp_path: Path) -> None:
    from feature_skills_webapp.storage.versions import record_version

    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    record_version(
        conn, ids["doc_a"], _make_content(), actor="test", now="2026-01-01T00:00:00+00:00"
    )
    record_version(
        conn, ids["doc_a"], _make_content(), actor="test", now="2026-01-02T00:00:00+00:00"
    )
    mark_diff_seen(conn, ids["doc_a"])
    assert acked_version(conn, ids["doc_a"]) == 2


def test_mark_diff_seen_updates_existing_acked_version(tmp_path: Path) -> None:
    from feature_skills_webapp.storage.versions import record_version

    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    record_version(
        conn, ids["doc_a"], _make_content(), actor="test", now="2026-01-01T00:00:00+00:00"
    )
    mark_diff_seen(conn, ids["doc_a"])
    assert acked_version(conn, ids["doc_a"]) == 1
    record_version(
        conn, ids["doc_a"], _make_content(), actor="test", now="2026-01-02T00:00:00+00:00"
    )
    mark_diff_seen(conn, ids["doc_a"])
    assert acked_version(conn, ids["doc_a"]) == 2


def test_mark_diff_seen_no_versions_sets_zero(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    mark_diff_seen(conn, ids["doc_a"])
    # No versions → MAX(version_num) is NULL, COALESCE(..., 0) = 0 → stored as 0
    row = conn.execute(
        "SELECT acked_version FROM read_state WHERE document_id=?", (ids["doc_a"],)
    ).fetchone()
    assert row is not None
    assert row["acked_version"] == 0


# --- has_unreviewed_changes ---


def test_has_unreviewed_changes_false_when_no_versions(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    assert has_unreviewed_changes(conn, ids["doc_a"]) is False


def test_has_unreviewed_changes_true_when_acked_behind_latest(tmp_path: Path) -> None:
    from feature_skills_webapp.storage.versions import record_version

    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    record_version(
        conn, ids["doc_a"], _make_content(), actor="test", now="2026-01-01T00:00:00+00:00"
    )
    record_version(
        conn, ids["doc_a"], _make_content(), actor="test", now="2026-01-02T00:00:00+00:00"
    )
    conn.execute(
        "INSERT INTO read_state (document_id, last_read_at, acked_version) VALUES (?, ?, 1)",
        (ids["doc_a"], "2026-01-01T00:00:00+00:00"),
    )
    assert has_unreviewed_changes(conn, ids["doc_a"]) is True


def test_has_unreviewed_changes_false_when_acked_equals_latest(tmp_path: Path) -> None:
    from feature_skills_webapp.storage.versions import record_version

    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    record_version(
        conn, ids["doc_a"], _make_content(), actor="test", now="2026-01-01T00:00:00+00:00"
    )
    record_version(
        conn, ids["doc_a"], _make_content(), actor="test", now="2026-01-02T00:00:00+00:00"
    )
    conn.execute(
        "INSERT INTO read_state (document_id, last_read_at, acked_version) VALUES (?, ?, 2)",
        (ids["doc_a"], "2026-01-02T00:00:00+00:00"),
    )
    assert has_unreviewed_changes(conn, ids["doc_a"]) is False


def test_has_unreviewed_changes_true_when_no_read_state_and_has_versions(tmp_path: Path) -> None:
    from feature_skills_webapp.storage.versions import record_version

    conn = temp_conn(tmp_path)
    with conn:
        ids = _seed(conn)
    record_version(
        conn, ids["doc_a"], _make_content(), actor="test", now="2026-01-01T00:00:00+00:00"
    )
    assert has_unreviewed_changes(conn, ids["doc_a"]) is True
