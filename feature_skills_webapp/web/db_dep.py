from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from feature_skills_webapp.storage.db import connect


@contextmanager
def request_conn(app) -> Iterator[sqlite3.Connection]:  # type: ignore[type-arg]
    """Open a per-request DB connection, closing it on exit."""
    db_path: Path = app.state.db_path
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
