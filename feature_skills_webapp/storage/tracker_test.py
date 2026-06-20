"""Tests for storage/tracker.py read accessors."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from feature_skills_webapp.storage.db import connect, migrate
from feature_skills_webapp.storage.tracker import (
    get_feature,
    get_project,
    list_feature_documents,
    list_features,
    list_projects,
)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    return conn


def _seed_project(conn: sqlite3.Connection, name: str) -> int:
    now = "2024-01-01T00:00:00+00:00"
    conn.execute("INSERT INTO projects (name, created_at) VALUES (?, ?)", (name, now))
    return conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()["id"]


def _seed_feature(
    conn: sqlite3.Connection,
    project_id: int,
    slug: str,
    *,
    status: str = "available",
    owner: str | None = None,
    notes: str | None = None,
) -> int:
    now = "2024-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO features (project_id, slug, status, owner, notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, slug, status, owner, notes, now, now),
    )
    return conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug=?", (project_id, slug)
    ).fetchone()["id"]


def _seed_doc(
    conn: sqlite3.Connection,
    project_id: int,
    feature_id: int | None,
    doc_type: str,
    instance: int = 1,
    *,
    status: str = "active",
    logical_key: str | None = None,
) -> int:
    now = "2024-01-01T00:00:00+00:00"
    lkey = logical_key or f"proj/{doc_type}/{instance}"
    conn.execute(
        "INSERT INTO documents "
        "(project_id, feature_id, type, instance, status, logical_key, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, feature_id, doc_type, instance, status, lkey, now, now),
    )
    return conn.execute(
        "SELECT id FROM documents WHERE project_id=? AND type=? AND instance=?",
        (project_id, doc_type, instance),
    ).fetchone()["id"]


def _seed_version(conn: sqlite3.Connection, doc_id: int, version_num: int) -> None:
    now = "2024-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO document_versions (document_id, version_num, content_json, actor, created_at) "
        "VALUES (?, ?, '{}', 'agent', ?)",
        (doc_id, version_num, now),
    )


# --- list_projects ---


def test_list_projects_empty(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    assert list_projects(conn) == []


def test_list_projects_ordered_by_name(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _seed_project(conn, "zebra")
    _seed_project(conn, "alpha")
    _seed_project(conn, "middle")
    names = [r["name"] for r in list_projects(conn)]
    assert names == ["alpha", "middle", "zebra"]


# --- get_project ---


def test_get_project_returns_row(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _seed_project(conn, "my-proj")
    row = get_project(conn, "my-proj")
    assert row is not None
    assert row["name"] == "my-proj"
    assert row["id"] is not None


def test_get_project_missing_returns_none(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    assert get_project(conn, "no-such") is None


# --- list_features ---


def test_list_features_returns_status_owner_notes(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat-a", status="available", owner="Alice", notes="some note")
    rows = list_features(conn, pid)
    assert len(rows) == 1
    row = rows[0]
    assert row["slug"] == "feat-a"
    assert row["status"] == "available"
    assert row["owner"] == "Alice"
    assert row["notes"] == "some note"


def test_list_features_empty(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    assert list_features(conn, pid) == []


def test_list_features_ordered_by_status_then_slug(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "z-done", status="done")
    _seed_feature(conn, pid, "a-done", status="done")
    _seed_feature(conn, pid, "b-available", status="available")
    _seed_feature(conn, pid, "a-in-progress", status="in_progress")
    slugs = [r["slug"] for r in list_features(conn, pid)]
    # ORDER BY status, slug — available < done < in_progress lexicographically
    assert slugs == ["b-available", "a-done", "z-done", "a-in-progress"]


# --- get_feature ---


def test_get_feature_returns_row(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat-x", status="in_progress", owner="Bob")
    row = get_feature(conn, "proj", "feat-x")
    assert row is not None
    assert row["slug"] == "feat-x"
    assert row["status"] == "in_progress"
    assert row["owner"] == "Bob"
    assert row["project"] == "proj"


def test_get_feature_missing_returns_none(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _seed_project(conn, "proj")
    assert get_feature(conn, "proj", "no-such") is None


def test_get_feature_wrong_project_returns_none(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj-a")
    _seed_project(conn, "proj-b")
    _seed_feature(conn, pid, "feat-x")
    assert get_feature(conn, "proj-b", "feat-x") is None


# --- list_feature_documents ---


def test_list_feature_documents_returns_active_only(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    fid = _seed_feature(conn, pid, "feat")
    active_id = _seed_doc(
        conn, pid, fid, "requirements", 1, status="active", logical_key="proj/feat/requirements/1"
    )
    _seed_doc(conn, pid, fid, "context", 1, status="archived", logical_key="proj/feat/context/1")
    _seed_doc(conn, pid, fid, "plan", 1, status="missing", logical_key="proj/feat/plan/1")

    rows = list_feature_documents(conn, fid)
    assert len(rows) == 1
    assert rows[0]["id"] == active_id


def test_list_feature_documents_excludes_project_level_tracker_doc(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    fid = _seed_feature(conn, pid, "feat")
    # feature_id IS NULL — project-level tracker doc
    _seed_doc(conn, pid, None, "features", 1, status="active", logical_key="proj/-/features/1")
    feat_doc_id = _seed_doc(
        conn, pid, fid, "context", 1, status="active", logical_key="proj/feat/context/1"
    )

    rows = list_feature_documents(conn, fid)
    assert len(rows) == 1
    assert rows[0]["id"] == feat_doc_id


def test_list_feature_documents_version_is_max_version_num(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    fid = _seed_feature(conn, pid, "feat")
    doc_id = _seed_doc(
        conn, pid, fid, "requirements", 1, status="active", logical_key="proj/feat/requirements/1"
    )
    _seed_version(conn, doc_id, 1)
    _seed_version(conn, doc_id, 2)
    _seed_version(conn, doc_id, 3)

    rows = list_feature_documents(conn, fid)
    assert rows[0]["version"] == 3


def test_list_feature_documents_version_zero_when_no_versions(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    fid = _seed_feature(conn, pid, "feat")
    _seed_doc(conn, pid, fid, "context", 1, status="active", logical_key="proj/feat/context/1")

    rows = list_feature_documents(conn, fid)
    assert rows[0]["version"] == 0


def test_list_feature_documents_empty(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    fid = _seed_feature(conn, pid, "feat")
    assert list_feature_documents(conn, fid) == []


def test_list_feature_documents_ordered_by_type_then_instance(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    fid = _seed_feature(conn, pid, "feat")
    _seed_doc(
        conn, pid, fid, "requirements", 2, status="active", logical_key="proj/feat/requirements/2"
    )
    _seed_doc(conn, pid, fid, "context", 1, status="active", logical_key="proj/feat/context/1")
    _seed_doc(
        conn, pid, fid, "requirements", 1, status="active", logical_key="proj/feat/requirements/1"
    )

    rows = list_feature_documents(conn, fid)
    types_instances = [(r["type"], r["instance"]) for r in rows]
    assert types_instances == [("context", 1), ("requirements", 1), ("requirements", 2)]
