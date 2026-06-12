"""Tests for storage/versions.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from feature_skills_webapp.storage.db import connect, migrate, now_iso
from feature_skills_webapp.storage.doc_content import ParsedContent, Section
from feature_skills_webapp.storage.versions import (
    backfill_logical_keys,
    current_content,
    record_version,
)


def _make_content(text: str = "body") -> ParsedContent:
    return ParsedContent(
        shape="sections", sections=(Section(key="overview", body=f"<p>{text}</p>"),)
    )


def _make_opaque(html: str = "<p>opaque</p>") -> ParsedContent:
    return ParsedContent(shape="opaque", sections=(Section(key="", body=html),))


def _temp_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    backfill_logical_keys(conn)
    return conn


def _seed_doc(
    conn: sqlite3.Connection,
    project: str = "proj",
    feature: str = "feat",
    doc_type: str = "context",
    logical_key: str | None = None,
    source_path: str = "/docs/proj/feat/context.html",
    status: str = "active",
) -> int:
    """Insert a minimal document row. Returns the document id."""
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES (?, ?)", (project, now))
    project_id = conn.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO features (project_id, slug, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (project_id, feature, now, now),
    )
    feature_id = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug=?", (project_id, feature)
    ).fetchone()["id"]
    lkey = logical_key if logical_key is not None else f"{project}/{feature}/{doc_type}/1"
    conn.execute(
        "INSERT INTO documents "
        "(project_id, feature_id, type, status, source_path, logical_key, instance, "
        "metadata_json, source_mtime, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, '{}', '2025-01-01T00:00:00+00:00', ?, ?)",
        (project_id, feature_id, doc_type, status, source_path, lkey, now, now),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


# --- record_version ---


def test_record_version_first_returns_1(tmp_path: Path) -> None:
    conn = _temp_conn(tmp_path)
    doc_id = _seed_doc(conn)
    ver = record_version(conn, doc_id, _make_content(), actor="test", now=now_iso())
    assert ver == 1


def test_record_version_increments(tmp_path: Path) -> None:
    conn = _temp_conn(tmp_path)
    doc_id = _seed_doc(conn)
    now = now_iso()
    v1 = record_version(conn, doc_id, _make_content("v1"), actor="test", now=now)
    v2 = record_version(conn, doc_id, _make_content("v2"), actor="test", now=now)
    assert v1 == 1
    assert v2 == 2


def test_record_version_stores_correct_json(tmp_path: Path) -> None:
    conn = _temp_conn(tmp_path)
    doc_id = _seed_doc(conn)
    content = _make_content("hello")
    record_version(conn, doc_id, content, actor="importer", now=now_iso())
    row = conn.execute(
        "SELECT content_json, actor FROM document_versions WHERE document_id=?", (doc_id,)
    ).fetchone()
    assert row is not None
    assert row["actor"] == "importer"
    import json

    data = json.loads(row["content_json"])
    assert data["shape"] == "sections"
    assert data["sections"][0]["key"] == "overview"
    assert "hello" in data["sections"][0]["body"]


def test_record_version_independent_per_document(tmp_path: Path) -> None:
    conn = _temp_conn(tmp_path)
    doc_a = _seed_doc(conn, feature="feat-a", logical_key="proj/feat-a/context/1")
    doc_b = _seed_doc(
        conn,
        feature="feat-b",
        logical_key="proj/feat-b/context/1",
        source_path="/docs/proj/feat-b/context.html",
    )
    now = now_iso()
    record_version(conn, doc_a, _make_content(), actor="test", now=now)
    v = record_version(conn, doc_b, _make_content(), actor="test", now=now)
    assert v == 1  # doc_b's own sequence starts at 1


# --- current_content ---


def test_current_content_none_for_no_versions(tmp_path: Path) -> None:
    conn = _temp_conn(tmp_path)
    doc_id = _seed_doc(conn)
    assert current_content(conn, doc_id) is None


def test_current_content_returns_latest(tmp_path: Path) -> None:
    conn = _temp_conn(tmp_path)
    doc_id = _seed_doc(conn)
    now = now_iso()
    record_version(conn, doc_id, _make_content("v1"), actor="test", now=now)
    record_version(conn, doc_id, _make_content("v2"), actor="test", now=now)
    got = current_content(conn, doc_id)
    assert got is not None
    assert got.sections[0].body == "<p>v2</p>"


def test_current_content_round_trips_opaque(tmp_path: Path) -> None:
    conn = _temp_conn(tmp_path)
    doc_id = _seed_doc(conn)
    original = _make_opaque("<h1>Hello</h1>")
    record_version(conn, doc_id, original, actor="test", now=now_iso())
    got = current_content(conn, doc_id)
    assert got is not None
    assert got.shape == "opaque"
    assert got.sections[0].body == "<h1>Hello</h1>"


# --- backfill_logical_keys ---


def _seed_legacy_doc(conn: sqlite3.Connection) -> int:
    """Insert a document row without logical_key (simulates pre-migration state)."""
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
    project_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO features (project_id, slug, created_at, updated_at) "
        "VALUES (?, 'feat', ?, ?)",
        (project_id, now, now),
    )
    feature_id = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat'", (project_id,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO documents "
        "(project_id, feature_id, type, status, source_path, "
        "metadata_json, source_mtime, created_at, updated_at) "
        "VALUES (?, ?, 'context', 'active', '/docs/proj/feat/context.html', "
        "'{}', '2025-01-01T00:00:00+00:00', ?, ?)",
        (project_id, feature_id, now, now),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def _conn_no_backfill(tmp_path: Path) -> sqlite3.Connection:
    """Fresh connection after migrate() but before backfill — for testing backfill itself."""
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    return conn


def test_backfill_assigns_logical_key(tmp_path: Path) -> None:
    conn = _conn_no_backfill(tmp_path)
    doc_id = _seed_legacy_doc(conn)
    backfill_logical_keys(conn)
    row = conn.execute(
        "SELECT logical_key, instance FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["logical_key"] == "proj/feat/context/1"
    assert row["instance"] == 1


def test_backfill_feedback_instance(tmp_path: Path) -> None:
    conn = _conn_no_backfill(tmp_path)
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
    project_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO features (project_id, slug, created_at, updated_at) "
        "VALUES (?, 'feat', ?, ?)",
        (project_id, now, now),
    )
    feature_id = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat'", (project_id,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO documents "
        "(project_id, feature_id, type, status, source_path, "
        "metadata_json, source_mtime, created_at, updated_at) "
        "VALUES (?, ?, 'requirements-feedback', 'active', "
        "'/docs/proj/feat/requirements-feedback-3.html', "
        "'{}', '2025-01-01T00:00:00+00:00', ?, ?)",
        (project_id, feature_id, now, now),
    )
    doc_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    backfill_logical_keys(conn)
    row = conn.execute(
        "SELECT logical_key, instance FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["logical_key"] == "proj/feat/requirements-feedback/3"
    assert row["instance"] == 3


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    conn = _conn_no_backfill(tmp_path)
    _seed_legacy_doc(conn)
    backfill_logical_keys(conn)
    backfill_logical_keys(conn)  # second call must not raise or corrupt data
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert count == 1


def test_backfill_creates_unique_index(tmp_path: Path) -> None:
    conn = _conn_no_backfill(tmp_path)
    _seed_legacy_doc(conn)
    backfill_logical_keys(conn)
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_documents_logical_key_unique'"
    ).fetchone()
    assert idx is not None


def test_backfill_drops_source_path_index(tmp_path: Path) -> None:
    conn = _conn_no_backfill(tmp_path)
    _seed_legacy_doc(conn)
    backfill_logical_keys(conn)
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_documents_source_path'"
    ).fetchone()
    assert idx is None


def test_backfill_resolves_collision_keeps_active(tmp_path: Path) -> None:
    """When two rows share a logical key, the active one (higher status rank) survives."""
    conn = _conn_no_backfill(tmp_path)
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
    project_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO features (project_id, slug, created_at, updated_at) "
        "VALUES (?, 'feat', ?, ?)",
        (project_id, now, now),
    )
    feature_id = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat'", (project_id,)
    ).fetchone()["id"]
    for status, path in [("active", "/a/context.html"), ("missing", "/b/context.html")]:
        conn.execute(
            "INSERT INTO documents "
            "(project_id, feature_id, type, status, source_path, "
            "metadata_json, source_mtime, created_at, updated_at) "
            "VALUES (?, ?, 'context', ?, ?, '{}', '2025-01-01T00:00:00+00:00', ?, ?)",
            (project_id, feature_id, status, path, now, now),
        )
    backfill_logical_keys(conn)
    rows = conn.execute("SELECT status FROM documents").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "active"


def test_backfill_repoints_read_state_to_survivor(tmp_path: Path) -> None:
    conn = _conn_no_backfill(tmp_path)
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
    project_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO features (project_id, slug, created_at, updated_at) "
        "VALUES (?, 'feat', ?, ?)",
        (project_id, now, now),
    )
    feature_id = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat'", (project_id,)
    ).fetchone()["id"]
    ids = []
    for status, path in [("active", "/a/context.html"), ("missing", "/b/context.html")]:
        conn.execute(
            "INSERT INTO documents "
            "(project_id, feature_id, type, status, source_path, "
            "metadata_json, source_mtime, created_at, updated_at) "
            "VALUES (?, ?, 'context', ?, ?, '{}', '2025-01-01T00:00:00+00:00', ?, ?)",
            (project_id, feature_id, status, path, now, now),
        )
        ids.append(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    # Add read_state on the loser (missing row)
    conn.execute(
        "INSERT INTO read_state (document_id, last_read_at) VALUES (?, '2025-06-01T10:00:00+00:00')",
        (ids[1],),
    )
    backfill_logical_keys(conn)
    survivor_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    rs = conn.execute(
        "SELECT last_read_at FROM read_state WHERE document_id=?", (survivor_id,)
    ).fetchone()
    assert rs is not None
    assert rs["last_read_at"] == "2025-06-01T10:00:00+00:00"


def test_backfill_fresh_db_no_rows(tmp_path: Path) -> None:
    """Backfill on an empty DB still creates the unique index."""
    conn = _conn_no_backfill(tmp_path)
    backfill_logical_keys(conn)
    idx = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_documents_logical_key_unique'"
    ).fetchone()
    assert idx is not None


def _seed_collision_pair(conn: sqlite3.Connection) -> tuple[int, int]:
    """Seed two pre-backfill rows that share one logical key (active + missing).

    Returns (active_id, missing_id); after backfill the active row is the survivor.
    """
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
    project_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO features (project_id, slug, created_at, updated_at) "
        "VALUES (?, 'feat', ?, ?)",
        (project_id, now, now),
    )
    feature_id = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat'", (project_id,)
    ).fetchone()["id"]
    ids: dict[str, int] = {}
    for status, path in [("active", "/a/context.html"), ("missing", "/b/context.html")]:
        conn.execute(
            "INSERT INTO documents "
            "(project_id, feature_id, type, status, source_path, "
            "metadata_json, source_mtime, created_at, updated_at) "
            "VALUES (?, ?, 'context', ?, ?, '{}', '2025-01-01T00:00:00+00:00', ?, ?)",
            (project_id, feature_id, status, path, now, now),
        )
        ids[status] = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return ids["active"], ids["missing"]


def test_backfill_merges_synthesis_responses_survivor_wins(tmp_path: Path) -> None:
    """Loser synthesis answers merge onto the survivor, but the survivor wins a per-item_num
    conflict (the documented-loss policy: ON CONFLICT(document_id, item_num) DO NOTHING)."""
    conn = _conn_no_backfill(tmp_path)
    survivor_id, loser_id = _seed_collision_pair(conn)
    now = now_iso()
    # Survivor already answered item 1; loser answered item 1 (conflict) and item 2 (new).
    for doc_id, item, resp in [
        (survivor_id, 1, "survivor-answer"),
        (loser_id, 1, "loser-answer"),
        (loser_id, 2, "loser-only"),
    ]:
        conn.execute(
            "INSERT INTO synthesis_responses "
            "(document_id, item_num, response, routine_flag, updated_at) "
            "VALUES (?, ?, ?, NULL, ?)",
            (doc_id, item, resp, now),
        )

    backfill_logical_keys(conn)

    rows = conn.execute(
        "SELECT item_num, response FROM synthesis_responses WHERE document_id=? ORDER BY item_num",
        (survivor_id,),
    ).fetchall()
    assert {r["item_num"]: r["response"] for r in rows} == {1: "survivor-answer", 2: "loser-only"}
    # Loser row deleted, so no orphaned synthesis rows remain.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM synthesis_responses WHERE document_id=?", (loser_id,)
        ).fetchone()["n"]
        == 0
    )


def test_backfill_repoints_comments_to_survivor(tmp_path: Path) -> None:
    """A comment on the losing row is repointed to the survivor, not cascade-deleted."""
    conn = _conn_no_backfill(tmp_path)
    survivor_id, loser_id = _seed_collision_pair(conn)
    conn.execute(
        "INSERT INTO comments (document_id, excerpt, text, status, created_at) "
        "VALUES (?, 'excerpt', 'a note', 'active', ?)",
        (loser_id, now_iso()),
    )
    backfill_logical_keys(conn)
    rows = conn.execute("SELECT document_id, text FROM comments").fetchall()
    assert len(rows) == 1
    assert rows[0]["document_id"] == survivor_id
    assert rows[0]["text"] == "a note"
