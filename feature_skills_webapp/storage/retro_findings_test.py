"""Tests for storage/retro_findings.py — read model and ordering."""

from __future__ import annotations

from pathlib import Path

from feature_skills_webapp.storage.db import connect, transaction
from feature_skills_webapp.storage.retro_findings import FindingRow, list_findings


def _setup(db_path: Path) -> tuple[int, int]:
    """Return (project_id, run_id) after inserting a project and a run."""
    conn = connect(db_path)
    now = "2026-01-01T00:00:00+00:00"
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
        proj_id: int = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
        conn.execute(
            "INSERT INTO retro_runs (project_id, run_key, created_at) VALUES (?, 'r1', ?)",
            (proj_id, now),
        )
        run_id: int = conn.execute("SELECT id FROM retro_runs WHERE run_key='r1'").fetchone()["id"]
    conn.close()
    return proj_id, run_id


def _insert_finding(
    db_path: Path,
    *,
    run_id: int,
    proj_id: int,
    title: str,
    status: str = "open",
    created_at: str = "2026-01-01T00:00:00+00:00",
    recurs_from: int | None = None,
) -> int:
    conn = connect(db_path)
    with transaction(conn):
        conn.execute(
            "INSERT INTO retro_findings "
            "(run_id, project_id, title, status, recurs_from, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, proj_id, title, status, recurs_from, created_at, created_at),
        )
        fid: int = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return fid


def test_list_findings_returns_all_statuses(temp_db: Path) -> None:
    proj_id, run_id = _setup(temp_db)
    for title, status in [("A", "open"), ("B", "actioned"), ("C", "rejected"), ("D", "deferred")]:
        _insert_finding(temp_db, run_id=run_id, proj_id=proj_id, title=title, status=status)

    conn = connect(temp_db)
    findings = list_findings(conn, proj_id)
    conn.close()

    titles = [f.title for f in findings]
    assert "A" in titles
    assert "B" in titles
    assert "C" in titles
    assert "D" in titles


def test_list_findings_recurring_sorts_first(temp_db: Path) -> None:
    proj_id, run_id = _setup(temp_db)

    # Insert parent and two non-recurring findings; parent gets one recurrence child
    parent_id = _insert_finding(
        temp_db,
        run_id=run_id,
        proj_id=proj_id,
        title="Parent",
        created_at="2026-01-02T00:00:00+00:00",
    )
    _insert_finding(
        temp_db,
        run_id=run_id,
        proj_id=proj_id,
        title="Older non-recurring",
        created_at="2026-01-01T00:00:00+00:00",
    )
    _insert_finding(
        temp_db,
        run_id=run_id,
        proj_id=proj_id,
        title="Child",
        recurs_from=parent_id,
        created_at="2026-01-03T00:00:00+00:00",
    )

    conn = connect(temp_db)
    findings = list_findings(conn, proj_id)
    conn.close()

    # "Parent" has recurrence_count=1, so it sorts before "Older non-recurring"
    titles = [f.title for f in findings]
    assert titles.index("Parent") < titles.index("Older non-recurring")


def test_list_findings_non_recurring_oldest_first(temp_db: Path) -> None:
    proj_id, run_id = _setup(temp_db)
    _insert_finding(
        temp_db,
        run_id=run_id,
        proj_id=proj_id,
        title="Newer",
        created_at="2026-06-01T00:00:00+00:00",
    )
    _insert_finding(
        temp_db,
        run_id=run_id,
        proj_id=proj_id,
        title="Older",
        created_at="2026-01-01T00:00:00+00:00",
    )

    conn = connect(temp_db)
    findings = list_findings(conn, proj_id)
    conn.close()

    titles = [f.title for f in findings]
    assert titles.index("Older") < titles.index("Newer")


def test_list_findings_id_tiebreak(temp_db: Path) -> None:
    """Same created_at: lower id comes first."""
    proj_id, run_id = _setup(temp_db)
    same_ts = "2026-03-01T00:00:00+00:00"
    fid_a = _insert_finding(
        temp_db, run_id=run_id, proj_id=proj_id, title="First inserted", created_at=same_ts
    )
    fid_b = _insert_finding(
        temp_db, run_id=run_id, proj_id=proj_id, title="Second inserted", created_at=same_ts
    )

    conn = connect(temp_db)
    findings = list_findings(conn, proj_id)
    conn.close()

    ids = [f.id for f in findings]
    assert ids.index(fid_a) < ids.index(fid_b)


def test_list_findings_empty_project(temp_db: Path) -> None:
    proj_id, _ = _setup(temp_db)
    conn = connect(temp_db)
    findings = list_findings(conn, proj_id)
    conn.close()
    assert findings == []


def test_list_findings_returns_finding_row_dataclass(temp_db: Path) -> None:
    proj_id, run_id = _setup(temp_db)
    _insert_finding(temp_db, run_id=run_id, proj_id=proj_id, title="Check type")

    conn = connect(temp_db)
    findings = list_findings(conn, proj_id)
    conn.close()

    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, FindingRow)
    assert f.title == "Check type"
    assert f.status == "open"
    assert f.recurrence_count == 0
    assert f.recurs_from is None
