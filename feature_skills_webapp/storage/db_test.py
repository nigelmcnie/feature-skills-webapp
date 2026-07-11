from pathlib import Path

import pytest

from feature_skills_webapp.storage.db import (
    MIGRATIONS_DIR,
    SchemaVersionMismatchError,
    connect,
    current_version,
    migrate,
    transaction,
)

EXPECTED_TABLES = {
    "projects",
    "features",
    "documents",
    "document_versions",
    "read_state",
    "synthesis_responses",
    "comments",
    "events",
    "schema_version",
    "retro_runs",
    "retro_findings",
}


def test_connect_wal(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()


def test_connect_foreign_keys(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    conn.close()


def test_migrate_fresh_returns_version_10(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    version = migrate(conn)
    assert version == 10
    conn.close()


def test_migrate_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    conn.close()

    conn = connect(db)
    version = migrate(conn)
    assert version == 10
    conn.close()


def test_schema_version_after_migrate(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    v = current_version(conn)
    assert v == 10
    conn.close()


def test_migration_0002_new_columns_and_indexes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    # project_id NOT NULL present (would error on insert without it)
    ts = "2026-01-01T00:00:00"
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('p1', ?)", (ts,))
        proj_id = conn.execute("SELECT id FROM projects WHERE name='p1'").fetchone()["id"]
        conn.execute(
            "INSERT INTO documents (project_id, type, created_at, updated_at) "
            "VALUES (?, 'context', ?, ?)",
            (proj_id, ts, ts),
        )
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert doc["project_id"] == proj_id
    assert doc["feature_id"] is None
    assert doc["status"] == "active"
    conn.close()


def test_migration_0002_indexes_exist(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    names = {r["name"] for r in rows}
    assert "idx_documents_project" in names
    assert "idx_documents_source_path" in names
    conn.close()


def test_migration_0002_source_path_unique(tmp_path: Path) -> None:
    import sqlite3

    conn = connect(tmp_path / "test.db")
    migrate(conn)
    ts = "2026-01-01T00:00:00"
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('p1', ?)", (ts,))
        proj_id = conn.execute("SELECT id FROM projects WHERE name='p1'").fetchone()["id"]
        conn.execute(
            "INSERT INTO documents (project_id, type, source_path, created_at, updated_at) "
            "VALUES (?, 'context', '/path/to/doc.html', ?, ?)",
            (proj_id, ts, ts),
        )
    with pytest.raises(sqlite3.IntegrityError), transaction(conn):
        conn.execute(
            "INSERT INTO documents (project_id, type, source_path, created_at, updated_at) "
            "VALUES (?, 'plan', '/path/to/doc.html', ?, ?)",
            (proj_id, ts, ts),
        )
    conn.close()


def test_migration_0002_documents_empty(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    count = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    assert count == 0
    conn.close()


def test_mismatch_raises(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    # Insert a version higher than any available migration (schema_version has PK on version).
    with transaction(conn):
        conn.execute("INSERT INTO schema_version (version) VALUES (9999)")
    with pytest.raises(SchemaVersionMismatchError):
        migrate(conn)
    conn.close()


def test_all_tables_present(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = {r["name"] for r in rows}
    assert tables >= EXPECTED_TABLES
    conn.close()


def test_tables_empty_after_migrate(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    for table in ("projects", "features", "documents", "events"):
        count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608
        assert count == 0, f"{table} should be empty"
    conn.close()


def test_fk_cascade_deletes_children(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('p1', '2026-01-01T00:00:00')")
        proj_id = conn.execute("SELECT id FROM projects WHERE name='p1'").fetchone()["id"]
        conn.execute(
            "INSERT INTO features (project_id, slug, created_at, updated_at) "
            "VALUES (?, 'f1', '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (proj_id,),
        )
    with transaction(conn):
        conn.execute("DELETE FROM projects WHERE id=?", (proj_id,))
    count = conn.execute("SELECT COUNT(*) AS n FROM features").fetchone()["n"]
    assert count == 0
    conn.close()


def test_events_survive_document_delete_with_null_fk(tmp_path: Path) -> None:
    """events.document_id is ON DELETE SET NULL: deleting a document must leave
    the audit-log row intact with a NULL document_id, not cascade it away."""
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    ts = "2026-01-01T00:00:00"
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('p1', ?)", (ts,))
        proj_id = conn.execute("SELECT id FROM projects WHERE name='p1'").fetchone()["id"]
        conn.execute(
            "INSERT INTO features (project_id, slug, created_at, updated_at) VALUES (?, 'f1', ?, ?)",
            (proj_id, ts, ts),
        )
        feat_id = conn.execute("SELECT id FROM features WHERE slug='f1'").fetchone()["id"]
        conn.execute(
            "INSERT INTO documents (project_id, feature_id, type, created_at, updated_at) "
            "VALUES (?, ?, 'context', ?, ?)",
            (proj_id, feat_id, ts, ts),
        )
        doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
        conn.execute(
            "INSERT INTO events (document_id, event_type, created_at) VALUES (?, 'discovered', ?)",
            (doc_id, ts),
        )
    with transaction(conn):
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    rows = conn.execute("SELECT document_id FROM events").fetchall()
    assert len(rows) == 1, "event row must survive document deletion"
    assert rows[0]["document_id"] is None, "document_id must be set NULL, not cascaded"
    conn.close()


def test_fk_indexes_present(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    names = {r["name"] for r in rows}
    assert {
        "idx_features_project",
        "idx_documents_feature",
        "idx_documents_project",
        "idx_events_document",
    } <= names
    conn.close()


def test_migrate_v1_to_v2_upgrade_path(tmp_path: Path) -> None:
    """Applying only 0001 yields the old documents shape at version 1; the full
    migrate() then upgrades in place to version 2 with the new shape (project_id,
    status). Exercises the real upgrade path, not just a fresh 0001+0002 build."""
    only_v1 = tmp_path / "migrations_v1"
    only_v1.mkdir()
    (only_v1 / "0001_init.sql").write_text((MIGRATIONS_DIR / "0001_init.sql").read_text())

    conn = connect(tmp_path / "test.db")
    assert migrate(conn, migrations_dir=only_v1) == 1
    cols_v1 = {r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    assert "status" not in cols_v1
    assert "project_id" not in cols_v1

    # Run the real migration set: upgrades 1 -> latest in place.
    assert migrate(conn) == 10
    assert current_version(conn) == 10
    cols_v2 = {r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    assert "status" in cols_v2
    assert "project_id" in cols_v2
    conn.close()


def test_migration_0008_adds_actor_with_agent_default(tmp_path: Path) -> None:
    """Fresh migrate() reaches the latest version; events.actor exists and new rows default to 'agent'."""
    conn = connect(tmp_path / "test.db")
    assert migrate(conn) == 10
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    assert "actor" in cols
    # An insert that omits actor takes the column default.
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'created', '{}', '2020-01-01T00:00:00+00:00')"
    )
    row = conn.execute("SELECT actor FROM events").fetchone()
    assert row["actor"] == "agent"
    conn.close()


def test_migration_0008_backfills_existing_comment_submitted_to_user(tmp_path: Path) -> None:
    """Events written before v8 backfill by type: comment_submitted → 'user',
    everything else → 'agent'."""
    # Build a DB at v7 (no actor column yet).
    pre_v8 = tmp_path / "migrations_pre_v8"
    pre_v8.mkdir()
    for src in sorted(MIGRATIONS_DIR.glob("000[1-7]_*.sql")):
        (pre_v8 / src.name).write_text(src.read_text())

    conn = connect(tmp_path / "test.db")
    assert migrate(conn, migrations_dir=pre_v8) == 7
    cols_v7 = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    assert "actor" not in cols_v7
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'comment_submitted', '{}', '2020-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'comment_integrated', '{}', '2020-01-02T00:00:00+00:00')"
    )

    # Now apply the real set through the latest version in place.
    assert migrate(conn) == 10
    by_type = {
        r["event_type"]: r["actor"]
        for r in conn.execute("SELECT event_type, actor FROM events").fetchall()
    }
    assert by_type["comment_submitted"] == "user"
    assert by_type["comment_integrated"] == "agent"
    conn.close()


def test_migration_0010_adds_archival_columns_nullable(tmp_path: Path) -> None:
    """Fresh migrate() reaches the latest version; the four archival columns exist and
    default to NULL."""
    conn = connect(tmp_path / "test.db")
    assert migrate(conn) == 10
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    assert {"archive_reason", "superseded_by", "archive_note", "archived_at"} <= cols

    ts = "2026-01-01T00:00:00+00:00"
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('p1', ?)", (ts,))
        proj_id = conn.execute("SELECT id FROM projects WHERE name='p1'").fetchone()["id"]
        conn.execute(
            "INSERT INTO documents (project_id, type, created_at, updated_at) "
            "VALUES (?, 'context', ?, ?)",
            (proj_id, ts, ts),
        )
    row = conn.execute(
        "SELECT archive_reason, superseded_by, archive_note, archived_at FROM documents"
    ).fetchone()
    assert row["archive_reason"] is None
    assert row["superseded_by"] is None
    assert row["archive_note"] is None
    assert row["archived_at"] is None
    conn.close()


def test_migration_0004_retro_tables_and_indexes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = {r["name"] for r in rows}
    assert "retro_runs" in tables
    assert "retro_findings" in tables

    idx_rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    indexes = {r["name"] for r in idx_rows}
    assert "idx_retro_runs_project" in indexes
    assert "idx_retro_findings_project" in indexes
    assert "idx_retro_findings_run" in indexes
    assert "idx_retro_findings_recurs_from" in indexes
    conn.close()


def test_transaction_rolls_back_on_exception(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    with pytest.raises(RuntimeError), transaction(conn):
        conn.execute(
            "INSERT INTO projects (name, created_at) VALUES ('rollback-me', '2026-01-01T00:00:00')"
        )
        raise RuntimeError("boom")
    n = conn.execute("SELECT COUNT(*) AS n FROM projects WHERE name='rollback-me'").fetchone()["n"]
    assert n == 0, "the failed transaction must have rolled back"
    conn.close()


# --- migration 0006: acked_version ---


def test_migration_0006_acked_version_column_exists(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(read_state)").fetchall()}
    assert "acked_version" in cols
    conn.close()


def test_migration_0006_backfills_acked_version_from_version_at_last_read(
    tmp_path: Path,
) -> None:
    """acked_version is backfilled to the latest version at-or-before last_read_at."""
    # Build a v5 DB by copying migrations 0001-0005 to a temp dir.
    only_v5 = tmp_path / "migrations_v5"
    only_v5.mkdir()
    v5_files = [
        "0001_init.sql",
        "0002_documents_status.sql",
        "0003_versioned_content.sql",
        "0004_retro_findings.sql",
        "0005_feature_status_backfill.sql",
    ]
    for name in v5_files:
        (only_v5 / name).write_text((MIGRATIONS_DIR / name).read_text())

    conn = connect(tmp_path / "test.db")
    assert migrate(conn, migrations_dir=only_v5) == 5

    ts_base = "2026-01-01T00:00:00+00:00"
    ts_v1 = "2026-03-01T00:00:00+00:00"
    ts_v2 = "2026-04-01T00:00:00+00:00"
    ts_read = "2026-03-15T00:00:00+00:00"  # between v1 and v2

    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('p', ?)", (ts_base,))
        proj_id = conn.execute("SELECT id FROM projects WHERE name='p'").fetchone()["id"]
        conn.execute(
            "INSERT INTO features (project_id, slug, created_at, updated_at) VALUES (?, 'f', ?, ?)",
            (proj_id, ts_base, ts_base),
        )
        feat_id = conn.execute("SELECT id FROM features WHERE slug='f'").fetchone()["id"]
        conn.execute(
            "INSERT INTO documents (project_id, feature_id, type, created_at, updated_at) "
            "VALUES (?, ?, 'context', ?, ?)",
            (proj_id, feat_id, ts_base, ts_base),
        )
        doc_id = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO document_versions (document_id, version_num, content_json, actor, created_at) "
            "VALUES (?, 1, '{}', 'test', ?)",
            (doc_id, ts_v1),
        )
        conn.execute(
            "INSERT INTO document_versions (document_id, version_num, content_json, actor, created_at) "
            "VALUES (?, 2, '{}', 'test', ?)",
            (doc_id, ts_v2),
        )
        conn.execute(
            "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?)",
            (doc_id, ts_read),
        )

    # Apply the remaining migrations by running the full migrate.
    assert migrate(conn) == 10

    row = conn.execute(
        "SELECT acked_version FROM read_state WHERE document_id = ?", (doc_id,)
    ).fetchone()
    assert row is not None
    # ts_v1 <= ts_read < ts_v2, so acked_version should be 1 (only v1 was before the read)
    assert row["acked_version"] == 1
    conn.close()


def test_migration_0006_never_read_row_stays_null(tmp_path: Path) -> None:
    """A doc with no read_state row gets no acked_version (NULL is the correct default)."""
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    ts = "2026-01-01T00:00:00+00:00"
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('p', ?)", (ts,))
        proj_id = conn.execute("SELECT id FROM projects WHERE name='p'").fetchone()["id"]
        conn.execute(
            "INSERT INTO features (project_id, slug, created_at, updated_at) VALUES (?, 'f', ?, ?)",
            (proj_id, ts, ts),
        )
        feat_id = conn.execute("SELECT id FROM features WHERE slug='f'").fetchone()["id"]
        conn.execute(
            "INSERT INTO documents (project_id, feature_id, type, created_at, updated_at) "
            "VALUES (?, ?, 'context', ?, ?)",
            (proj_id, feat_id, ts, ts),
        )
        doc_id = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
    row = conn.execute(
        "SELECT acked_version FROM read_state WHERE document_id = ?", (doc_id,)
    ).fetchone()
    assert row is None
    conn.close()


# ---------------------------------------------------------------------------
# migration file collision guard
# ---------------------------------------------------------------------------


def test_migration_stems_are_unique_and_gap_free_through_current_version(tmp_path: Path) -> None:
    """Migration filenames must parse to a unique, contiguous 1..N sequence.

    migrate() silently skips any file whose leading number is <= the applied
    schema_version (see storage/db.py). Two migrations landing with the same
    number is invisible until a query against the never-applied ALTER TABLE
    fails at runtime -- this test turns that into a loud CI failure instead.
    """
    versions = sorted(int(p.stem.split("_", 1)[0]) for p in MIGRATIONS_DIR.glob("*.sql"))
    assert len(versions) == len(set(versions)), f"duplicate migration numbers: {versions}"
    assert versions == list(range(1, len(versions) + 1)), f"gap in migration sequence: {versions}"
    conn = connect(tmp_path / "test.db")
    assert migrate(conn) == versions[-1]
    conn.close()
