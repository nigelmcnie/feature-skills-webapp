"""Tests for storage/tracker.py read accessors and mutations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from feature_skills_webapp.storage.db import MIGRATIONS_DIR, connect, migrate, transaction
from feature_skills_webapp.storage.tracker import (
    FeatureExists,
    FeatureNotFound,
    InvalidTransition,
    capture_feature,
    claim_feature,
    drop_feature,
    get_feature,
    get_project,
    list_feature_documents,
    list_features,
    list_projects,
    normalise_feature_slugs,
    park_feature,
    release_feature,
    ship_feature,
    update_feature_note,
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


# ---------------------------------------------------------------------------
# Migration: backfill NULL status
# ---------------------------------------------------------------------------


def test_migration_backfills_null_status(tmp_path: Path) -> None:
    # Build a pre-0005 DB so migrate() runs 0005 (and 0006) from scratch on real data.
    only_v4 = tmp_path / "migrations_v4"
    only_v4.mkdir()
    for name in [
        "0001_init.sql",
        "0002_documents_status.sql",
        "0003_versioned_content.sql",
        "0004_retro_findings.sql",
    ]:
        (only_v4 / name).write_text((MIGRATIONS_DIR / name).read_text())

    conn = connect(tmp_path / "test.db")
    migrate(conn, migrations_dir=only_v4)

    pid = _seed_project(conn, "proj")
    # Force a NULL status row (bypassing the default) to simulate pre-migration state.
    conn.execute(
        "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
        "VALUES (?, 'feat', NULL, '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')",
        (pid,),
    )
    row = conn.execute(
        "SELECT status FROM features WHERE slug='feat' AND project_id=?", (pid,)
    ).fetchone()
    assert row["status"] is None

    # Full migrate() applies 0005 (backfill) and 0006 (acked_version column).
    assert migrate(conn) == 6
    row = conn.execute(
        "SELECT status FROM features WHERE slug='feat' AND project_id=?", (pid,)
    ).fetchone()
    assert row["status"] == "available"


def test_upsert_feature_seeds_available_status(tmp_path: Path) -> None:
    from feature_skills_webapp.storage.walker import upsert_feature, upsert_project

    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = upsert_project(conn, "proj", now)
    upsert_feature(conn, pid, "brand-new", now)
    row = conn.execute(
        "SELECT status FROM features WHERE project_id=? AND slug='brand-new'", (pid,)
    ).fetchone()
    assert row["status"] == "available"


# ---------------------------------------------------------------------------
# capture_feature
# ---------------------------------------------------------------------------


def test_capture_creates_available_feature_and_event(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    with transaction(conn):
        result = capture_feature(conn, project="proj", slug="new-feat", notes="n", now=now)
    assert result.status == "available"
    assert result.changed is True
    feat = get_feature(conn, "proj", "new-feat")
    assert feat is not None
    assert feat["status"] == "available"
    assert feat["notes"] == "n"
    event = conn.execute(
        "SELECT event_type FROM events WHERE event_type='feature_created'"
    ).fetchone()
    assert event is not None


def test_capture_raises_feature_exists_on_duplicate(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    with transaction(conn):
        capture_feature(conn, project="proj", slug="feat", notes=None, now=now)
    with pytest.raises(FeatureExists), transaction(conn):
        capture_feature(conn, project="proj", slug="feat", notes=None, now=now)


def test_capture_with_no_notes(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    with transaction(conn):
        result = capture_feature(conn, project="proj", slug="feat", notes=None, now=now)
    assert result.status == "available"
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["notes"] is None


# ---------------------------------------------------------------------------
# claim_feature
# ---------------------------------------------------------------------------


def test_claim_available_transitions_to_in_progress(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="available")
    with transaction(conn):
        result = claim_feature(conn, project="proj", slug="feat", owner="Alice", now=now)
    assert result.status == "in_progress"
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "in_progress"
    assert feat["owner"] == "Alice"
    event = conn.execute(
        "SELECT event_type FROM events WHERE event_type='feature_claimed'"
    ).fetchone()
    assert event is not None


def test_claim_already_in_progress_is_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="in_progress")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with transaction(conn):
        result = claim_feature(conn, project="proj", slug="feat", owner="Alice", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert result.changed is False
    assert result.status == "in_progress"
    assert after == before  # no event emitted


def test_claim_done_feature_raises_invalid_transition(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="done")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with pytest.raises(InvalidTransition), transaction(conn):
        claim_feature(conn, project="proj", slug="feat", owner="Alice", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert after == before  # no event emitted on rejection
    # Status unchanged
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "done"


def test_claim_missing_feature_raises_feature_not_found(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    _seed_project(conn, "proj")
    with pytest.raises(FeatureNotFound), transaction(conn):
        claim_feature(conn, project="proj", slug="no-such", owner="Alice", now=now)


# ---------------------------------------------------------------------------
# ship_feature
# ---------------------------------------------------------------------------


def test_ship_in_progress_transitions_to_done(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="in_progress")
    with transaction(conn):
        result = ship_feature(conn, project="proj", slug="feat", outcome="great", now=now)
    assert result.status == "done"
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "done"
    assert feat["notes"] == "great"
    event = conn.execute("SELECT event_type FROM events WHERE event_type='shipped'").fetchone()
    assert event is not None


def test_ship_writes_outcome_to_notes(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="in_progress", notes="old note")
    with transaction(conn):
        ship_feature(conn, project="proj", slug="feat", outcome="new outcome", now=now)
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["notes"] == "new outcome"


def test_ship_without_outcome_leaves_notes_unchanged(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="in_progress", notes="existing note")
    with transaction(conn):
        ship_feature(conn, project="proj", slug="feat", outcome=None, now=now)
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["notes"] == "existing note"


def test_ship_already_done_is_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="done")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with transaction(conn):
        result = ship_feature(conn, project="proj", slug="feat", outcome=None, now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert result.changed is False
    assert result.status == "done"
    assert after == before  # no event emitted


def test_ship_available_feature_backfills_to_done(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="available")
    with transaction(conn):
        result = ship_feature(conn, project="proj", slug="feat", outcome=None, now=now)
    assert result.status == "done"
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "done"
    event = conn.execute("SELECT event_type FROM events WHERE event_type='shipped'").fetchone()
    assert event is not None


def test_ship_parked_feature_raises_invalid_transition(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="parked")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with pytest.raises(InvalidTransition), transaction(conn):
        ship_feature(conn, project="proj", slug="feat", outcome=None, now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert after == before
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "parked"


def test_ship_missing_feature_raises_feature_not_found(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    _seed_project(conn, "proj")
    with pytest.raises(FeatureNotFound), transaction(conn):
        ship_feature(conn, project="proj", slug="no-such", outcome=None, now=now)


# ---------------------------------------------------------------------------
# release_feature
# ---------------------------------------------------------------------------


def test_release_in_progress_transitions_to_available(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="in_progress", owner="Alice")
    with transaction(conn):
        result = release_feature(conn, project="proj", slug="feat", now=now)
    assert result.status == "available"
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "available"
    assert feat["owner"] is None
    event = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='feature_released'"
    ).fetchone()
    assert event is not None
    payload = json.loads(event["payload_json"])
    assert payload["owner"] == "Alice"
    assert payload["project"] == "proj"
    assert payload["slug"] == "feat"


def test_release_already_available_is_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="available")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with transaction(conn):
        result = release_feature(conn, project="proj", slug="feat", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert result.changed is False
    assert result.status == "available"
    assert after == before


def test_release_done_feature_raises_invalid_transition(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="done")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with pytest.raises(InvalidTransition), transaction(conn):
        release_feature(conn, project="proj", slug="feat", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert after == before
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "done"


def test_release_parked_feature_raises_invalid_transition(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="parked")
    with pytest.raises(InvalidTransition), transaction(conn):
        release_feature(conn, project="proj", slug="feat", now=now)


def test_release_missing_feature_raises_feature_not_found(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    _seed_project(conn, "proj")
    with pytest.raises(FeatureNotFound), transaction(conn):
        release_feature(conn, project="proj", slug="no-such", now=now)


# ---------------------------------------------------------------------------
# park_feature
# ---------------------------------------------------------------------------


def test_park_available_transitions_to_parked(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="available", owner="Alice")
    with transaction(conn):
        result = park_feature(conn, project="proj", slug="feat", now=now)
    assert result.status == "parked"
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "parked"
    assert feat["owner"] is None
    event = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='feature_parked'"
    ).fetchone()
    assert event is not None
    payload = json.loads(event["payload_json"])
    assert payload["owner"] == "Alice"
    assert payload["project"] == "proj"
    assert payload["slug"] == "feat"


def test_park_in_progress_transitions_to_parked(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="in_progress", owner="Bob")
    with transaction(conn):
        result = park_feature(conn, project="proj", slug="feat", now=now)
    assert result.status == "parked"
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "parked"
    assert feat["owner"] is None
    event = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='feature_parked'"
    ).fetchone()
    assert event is not None
    payload = json.loads(event["payload_json"])
    assert payload["owner"] == "Bob"


def test_park_already_parked_is_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="parked")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with transaction(conn):
        result = park_feature(conn, project="proj", slug="feat", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert result.changed is False
    assert result.status == "parked"
    assert after == before


def test_park_done_feature_raises_invalid_transition(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="done")
    with pytest.raises(InvalidTransition), transaction(conn):
        park_feature(conn, project="proj", slug="feat", now=now)
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "done"


def test_park_missing_feature_raises_feature_not_found(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    _seed_project(conn, "proj")
    with pytest.raises(FeatureNotFound), transaction(conn):
        park_feature(conn, project="proj", slug="no-such", now=now)


def test_claim_parked_feature_resumes_as_in_progress(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="parked")
    with transaction(conn):
        result = claim_feature(conn, project="proj", slug="feat", owner="Charlie", now=now)
    assert result.status == "in_progress"
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "in_progress"
    assert feat["owner"] == "Charlie"
    event = conn.execute(
        "SELECT event_type FROM events WHERE event_type='feature_claimed'"
    ).fetchone()
    assert event is not None
    # No feature_resumed event — resume reuses feature_claimed
    resumed = conn.execute(
        "SELECT event_type FROM events WHERE event_type='feature_resumed'"
    ).fetchone()
    assert resumed is None


# --- guard: slug normalisation on the mutation/lookup boundary ---


def test_capture_normalises_display_name_to_slug(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    with transaction(conn):
        result = capture_feature(
            conn, project="proj", slug="File Classification", notes=None, now=now
        )
    assert result.slug == "file-classification"
    stored = conn.execute("SELECT slug FROM features").fetchall()
    assert [r["slug"] for r in stored] == ["file-classification"]


def test_capture_of_display_name_then_slug_is_a_duplicate(tmp_path: Path) -> None:
    # The core regression: a display-name capture and a kebab capture must not
    # become two rows. Without slugify the second capture would not collide.
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    with transaction(conn):
        capture_feature(conn, project="proj", slug="File classification", notes=None, now=now)
    with pytest.raises(FeatureExists), transaction(conn):
        capture_feature(conn, project="proj", slug="file-classification", notes=None, now=now)
    assert conn.execute("SELECT COUNT(*) AS n FROM features").fetchone()["n"] == 1


def test_get_feature_matches_non_canonical_input(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "file-classification")
    feat = get_feature(conn, "proj", "File Classification")
    assert feat is not None
    assert feat["slug"] == "file-classification"


def test_claim_and_ship_resolve_via_non_canonical_slug(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "file-classification")
    with transaction(conn):
        claim_feature(conn, project="proj", slug="File classification", owner="Nigel", now=now)
    with transaction(conn):
        ship_feature(conn, project="proj", slug="FILE   classification", outcome=None, now=now)
    rows = conn.execute("SELECT slug, status FROM features").fetchall()
    assert len(rows) == 1
    assert rows[0]["slug"] == "file-classification"
    assert rows[0]["status"] == "done"


# --- backfill: normalise_feature_slugs ---


def test_normalise_renames_non_canonical_slug(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "File classification", status="in_progress")
    with transaction(conn):
        report = normalise_feature_slugs(conn)
    assert report.renamed == [("proj", "File classification", "file-classification")]
    assert report.conflicts == []
    row = conn.execute("SELECT slug, status FROM features").fetchone()
    assert row["slug"] == "file-classification"
    assert row["status"] == "in_progress"  # status untouched


def test_normalise_is_idempotent(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "Markdown checks")
    with transaction(conn):
        normalise_feature_slugs(conn)
    with transaction(conn):
        second = normalise_feature_slugs(conn)
    assert second.renamed == []
    assert second.conflicts == []


def test_normalise_rewrites_document_logical_keys(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    fid = _seed_feature(conn, pid, "File classification")
    _seed_doc(conn, pid, fid, "requirements", logical_key="proj/File classification/requirements/1")
    with transaction(conn):
        normalise_feature_slugs(conn)
    lkey = conn.execute("SELECT logical_key FROM documents").fetchone()["logical_key"]
    assert lkey == "proj/file-classification/requirements/1"


def test_normalise_reports_collision_without_mutating(tmp_path: Path) -> None:
    # A non-canonical 'done' shell colliding with a canonical 'available' row is
    # a judgement call — report it, change nothing.
    conn = _conn(tmp_path)
    pid = _seed_project(conn, "proj")
    shell = _seed_feature(conn, pid, "Synthesis verify+retry v2", status="done")
    canon = _seed_feature(conn, pid, "synthesis-verify-retry-v2", status="available")
    with transaction(conn):
        report = normalise_feature_slugs(conn)
    assert report.renamed == []
    assert len(report.conflicts) == 1
    c = report.conflicts[0]
    assert (c.old_id, c.old_status, c.target_id, c.target_status) == (
        shell,
        "done",
        canon,
        "available",
    )
    # both rows still present, unchanged
    slugs = {r["slug"] for r in conn.execute("SELECT slug FROM features").fetchall()}
    assert slugs == {"Synthesis verify+retry v2", "synthesis-verify-retry-v2"}


# ---------------------------------------------------------------------------
# drop_feature
# ---------------------------------------------------------------------------


def test_drop_available_transitions_to_archived(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="available")
    with transaction(conn):
        result = drop_feature(conn, project="proj", slug="feat", now=now)
    assert result.status == "archived"
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "archived"
    event = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='feature_dropped'"
    ).fetchone()
    assert event is not None
    payload = json.loads(event["payload_json"])
    assert payload["project"] == "proj"
    assert payload["slug"] == "feat"


def test_drop_in_progress_transitions_to_archived_and_retains_owner(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="in_progress", owner="Alice")
    with transaction(conn):
        result = drop_feature(conn, project="proj", slug="feat", now=now)
    assert result.status == "archived"
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "archived"
    assert feat["owner"] == "Alice"  # owner retained
    event = conn.execute(
        "SELECT event_type FROM events WHERE event_type='feature_dropped'"
    ).fetchone()
    assert event is not None


def test_drop_done_raises_invalid_transition_no_event(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="done")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with pytest.raises(InvalidTransition), transaction(conn):
        drop_feature(conn, project="proj", slug="feat", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert after == before  # no event emitted
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "done"  # status unchanged


def test_drop_already_archived_is_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="archived")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with transaction(conn):
        result = drop_feature(conn, project="proj", slug="feat", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert result.changed is False
    assert result.status == "archived"
    assert after == before  # no event emitted


def test_drop_missing_feature_raises_feature_not_found(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    _seed_project(conn, "proj")
    with pytest.raises(FeatureNotFound), transaction(conn):
        drop_feature(conn, project="proj", slug="no-such", now=now)


# ---------------------------------------------------------------------------
# update_feature_note
# ---------------------------------------------------------------------------


def test_update_feature_note_changes_note_and_emits_event(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = (
        "2024-06-01T00:00:00+00:00"  # later than the seed timestamp, so updated_at visibly advances
    )
    pid = _seed_project(conn, "proj")
    fid = _seed_feature(conn, pid, "feat", status="available", notes="old")
    with transaction(conn):
        result = update_feature_note(conn, project="proj", slug="feat", notes="new", now=now)
    assert result.changed is True
    assert result.status == "available"
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["notes"] == "new"
    # Exactly one event, carrying the same payload shape as the sibling mutations.
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='feature_note_updated'"
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"]) == {"project": "proj", "slug": "feat"}
    # updated_at is bumped to `now` on a real change.
    updated_at = conn.execute("SELECT updated_at FROM features WHERE id=?", (fid,)).fetchone()[0]
    assert updated_at == now


def test_update_feature_note_identical_is_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-06-01T00:00:00+00:00"  # later than the seed; a no-op must NOT write it
    pid = _seed_project(conn, "proj")
    fid = _seed_feature(conn, pid, "feat", notes="same")
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with transaction(conn):
        result = update_feature_note(conn, project="proj", slug="feat", notes="same", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert result.changed is False
    assert after == before
    # updated_at untouched on a no-op — still the seed timestamp, not `now`.
    updated_at = conn.execute("SELECT updated_at FROM features WHERE id=?", (fid,)).fetchone()[0]
    assert updated_at == "2024-01-01T00:00:00+00:00"


def test_update_feature_note_done_preserves_status(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", status="done", notes="old")
    with transaction(conn):
        result = update_feature_note(conn, project="proj", slug="feat", notes="updated", now=now)
    assert result.changed is True
    assert result.status == "done"
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["status"] == "done"
    assert feat["notes"] == "updated"


def test_update_feature_note_missing_raises_feature_not_found(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    _seed_project(conn, "proj")
    with pytest.raises(FeatureNotFound), transaction(conn):
        update_feature_note(conn, project="proj", slug="no-such", notes="x", now=now)


def test_update_feature_note_fills_null(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", notes=None)
    with transaction(conn):
        result = update_feature_note(conn, project="proj", slug="feat", notes="filled", now=now)
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["notes"] == "filled"


def test_update_feature_note_null_to_empty_is_change(tmp_path: Path) -> None:
    # A NULL note (Python None) compared against "" must be a real change, not a
    # silent no-op — pins the None != "" boundary. If get_feature ever coerced
    # NULL to "", this is the test that would go red.
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", notes=None)
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with transaction(conn):
        result = update_feature_note(conn, project="proj", slug="feat", notes="", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert result.changed is True
    assert after == before + 1
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["notes"] == ""


def test_update_feature_note_empty_clears_and_empty_again_is_noop(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"
    pid = _seed_project(conn, "proj")
    _seed_feature(conn, pid, "feat", notes="something")
    with transaction(conn):
        result = update_feature_note(conn, project="proj", slug="feat", notes="", now=now)
    assert result.changed is True
    feat = get_feature(conn, "proj", "feat")
    assert feat is not None
    assert feat["notes"] == ""
    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    with transaction(conn):
        result2 = update_feature_note(conn, project="proj", slug="feat", notes="", now=now)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert result2.changed is False
    assert after == before
