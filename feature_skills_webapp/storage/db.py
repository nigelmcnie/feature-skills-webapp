"""SQLite connection and migration runner (near-direct port of kea's storage/db.py)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class SchemaVersionMismatchError(Exception):
    """DB schema_version exceeds the highest available migration.

    The DB was created by a newer version of feature-skills-webapp. Delete it and re-run.
    """


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection with performance pragmas, FK enforcement, and row factory configured.

    Uses isolation_level=None (autocommit) so transactions are driven explicitly
    via the transaction() context manager — the stdlib ``with conn:`` form is a
    silent no-op under autocommit and must NOT be used.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # executescript issues an implicit COMMIT before running, which is required
    # for PRAGMA journal_mode and PRAGMA foreign_keys (both fail inside a txn).
    conn.executescript(
        "PRAGMA journal_mode=WAL;"
        "PRAGMA synchronous=NORMAL;"
        "PRAGMA temp_store=MEMORY;"
        "PRAGMA cache_size=-20000;"
        "PRAGMA mmap_size=67108864;"
        "PRAGMA foreign_keys=ON;"
        # transaction() uses BEGIN IMMEDIATE, which grabs the writer lock eagerly.
        # With the default busy_timeout of 0, the loser of any two-writer race fails
        # instantly with OperationalError('database is locked') — flaky under load.
        # Wait up to 5s for the lock instead; a genuine deadlock still surfaces after.
        "PRAGMA busy_timeout=5000;"
    )
    # Runtime guard: journal_mode=WAL silently no-ops inside an open transaction
    # and SQLite returns the current mode instead.  Assert we actually landed.
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() != "wal":
        raise RuntimeError(f"Expected journal_mode=wal, got {mode!r}")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("Expected foreign_keys=ON after connect()")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK context manager.

    Required because isolation_level=None (autocommit) makes the stdlib
    ``with conn:`` form a silent no-op.  BEGIN IMMEDIATE acquires the writer
    lock at BEGIN time so contention surfaces fast and deterministically.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> int:
    """Apply all pending migrations. Returns the resulting schema version.

    Holds BEGIN IMMEDIATE for the full check-and-apply cycle so concurrent
    processes opening a fresh DB don't race on CREATE TABLE.

    Raises SchemaVersionMismatchError if the DB's schema_version exceeds the
    highest available migration.

    NOTE: each migration file is split on ';' naively — fine for the plain DDL
    in the current migrations, but a sharp edge for any future migration containing
    triggers or semicolons inside string literals.
    """
    with transaction(conn):
        applied = current_version(conn)
        migration_files = sorted(migrations_dir.glob("*.sql"))
        if migration_files:
            max_available = max(int(p.stem.split("_", 1)[0]) for p in migration_files)
            if applied > max_available:
                raise SchemaVersionMismatchError(
                    f"schema_version={applied} in DB but only migrations through "
                    f"version {max_available} exist. This DB was created by a newer "
                    f"version of feature-skills-webapp. Delete it and re-run."
                )
        for path in migration_files:
            version = int(path.stem.split("_", 1)[0])
            if version <= applied:
                continue
            for stmt in (s.strip() for s in path.read_text().split(";")):
                if stmt:
                    conn.execute(stmt)
            applied = version
    return applied


@contextmanager
def open_db(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a migrated DB connection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        migrate(conn)
        yield conn
    finally:
        conn.close()
