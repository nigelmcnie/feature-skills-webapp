from pathlib import Path

import pytest

from feature_skills_webapp.storage.db import (
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
    "read_state",
    "synthesis_responses",
    "comments",
    "events",
    "schema_version",
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


def test_migrate_fresh_returns_version_1(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    version = migrate(conn)
    assert version == 1
    conn.close()


def test_migrate_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    conn.close()

    conn = connect(db)
    version = migrate(conn)
    assert version == 1
    conn.close()


def test_schema_version_after_migrate(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    v = current_version(conn)
    assert v == 1
    conn.close()


def test_mismatch_raises(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    migrate(conn)
    # Stamp a version higher than any available migration.
    with transaction(conn):
        conn.execute("UPDATE schema_version SET version = 9999")
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
