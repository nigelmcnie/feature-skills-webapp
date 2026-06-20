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


def test_migrate_fresh_returns_version_5(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    version = migrate(conn)
    assert version == 5
    conn.close()


def test_migrate_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    conn.close()

    conn = connect(db)
    version = migrate(conn)
    assert version == 5
    conn.close()


def test_schema_version_after_migrate(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    v = current_version(conn)
    assert v == 5
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

    # Run the real migration set: upgrades 1 -> 5 in place.
    assert migrate(conn) == 5
    assert current_version(conn) == 5
    cols_v2 = {r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    assert "status" in cols_v2
    assert "project_id" in cols_v2
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
