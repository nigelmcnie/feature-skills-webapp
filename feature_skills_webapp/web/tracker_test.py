"""Tests for web/tracker.py GET listing and POST mutation handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from feature_skills_webapp.storage.db import connect
from feature_skills_webapp.web.app import create_app


def _seed(db: Path) -> None:
    """Seed two projects, features in mixed statuses, one document with versions."""
    conn = connect(db)
    now = "2024-01-01T00:00:00+00:00"

    conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (now,))
    proj_a = conn.execute("SELECT id FROM projects WHERE name='proj-a'").fetchone()["id"]
    conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-b', ?)", (now,))

    conn.execute(
        "INSERT INTO features (project_id, slug, status, owner, notes, created_at, updated_at) "
        "VALUES (?, 'feat-one', 'available', 'Alice', 'some note', ?, ?)",
        (proj_a, now, now),
    )
    feat_one = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat-one'", (proj_a,)
    ).fetchone()["id"]

    conn.execute(
        "INSERT INTO features (project_id, slug, status, owner, notes, created_at, updated_at) "
        "VALUES (?, 'feat-two', 'in_progress', NULL, NULL, ?, ?)",
        (proj_a, now, now),
    )

    # Active doc with 2 versions
    conn.execute(
        "INSERT INTO documents "
        "(project_id, feature_id, type, instance, status, logical_key, created_at, updated_at) "
        "VALUES (?, ?, 'requirements', 1, 'active', 'proj-a/feat-one/requirements/1', ?, ?)",
        (proj_a, feat_one, now, now),
    )
    doc_id = conn.execute(
        "SELECT id FROM documents WHERE logical_key='proj-a/feat-one/requirements/1'"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO document_versions (document_id, version_num, content_json, actor, created_at) "
        "VALUES (?, 1, '{}', 'agent', ?)",
        (doc_id, now),
    )
    conn.execute(
        "INSERT INTO document_versions (document_id, version_num, content_json, actor, created_at) "
        "VALUES (?, 2, '{}', 'agent', ?)",
        (doc_id, now),
    )

    # Archived doc — should be excluded from documents listing
    conn.execute(
        "INSERT INTO documents "
        "(project_id, feature_id, type, instance, status, logical_key, created_at, updated_at) "
        "VALUES (?, ?, 'context', 1, 'archived', 'proj-a/feat-one/context/1', ?, ?)",
        (proj_a, feat_one, now, now),
    )

    conn.commit()
    conn.close()


# --- 503 when no DB ---


def test_list_projects_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/projects")
    assert resp.status_code == 503


def test_list_features_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/projects/proj-a/features")
    assert resp.status_code == 503


def test_list_documents_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/projects/proj-a/features/feat-one/documents")
    assert resp.status_code == 503


# --- list_projects ---


def test_list_projects_200_shape(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert "projects" in data
    names = [p["name"] for p in data["projects"]]
    assert "proj-a" in names
    assert "proj-b" in names


def test_list_projects_empty_db(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json()["projects"] == []


# --- list_features ---


def test_list_features_200_shape(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/proj-a/features")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "proj-a"
    slugs = [f["slug"] for f in data["features"]]
    assert "feat-one" in slugs
    assert "feat-two" in slugs


def test_list_features_includes_status_owner_notes(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/proj-a/features")
    feats = {f["slug"]: f for f in resp.json()["features"]}
    assert feats["feat-one"]["status"] == "available"
    assert feats["feat-one"]["owner"] == "Alice"
    assert feats["feat-one"]["notes"] == "some note"
    assert feats["feat-two"]["status"] == "in_progress"
    assert feats["feat-two"]["owner"] is None


def test_list_features_404_unknown_project(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/no-such/features")
    assert resp.status_code == 404


def test_list_features_200_empty_project(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/proj-b/features")
    assert resp.status_code == 200
    assert resp.json()["features"] == []


# --- list_documents ---


def test_list_documents_200_shape(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/proj-a/features/feat-one/documents")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "proj-a"
    assert data["feature"] == "feat-one"
    assert len(data["documents"]) == 1
    doc = data["documents"][0]
    assert doc["doc_type"] == "requirements"
    assert doc["instance"] == 1
    assert doc["logical_key"] == "proj-a/feat-one/requirements/1"
    assert doc["version"] == 2
    assert "document_id" in doc
    assert doc["url"] == f"/doc/{doc['document_id']}"


def test_list_documents_excludes_archived(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/proj-a/features/feat-one/documents")
    doc_types = [d["doc_type"] for d in resp.json()["documents"]]
    assert "context" not in doc_types


def test_list_documents_200_empty_when_no_docs(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/proj-a/features/feat-two/documents")
    assert resp.status_code == 200
    assert resp.json()["documents"] == []


def test_list_documents_404_unknown_feature(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/proj-a/features/no-such/documents")
    assert resp.status_code == 404


def test_list_documents_404_unknown_project(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/no-such/features/feat-one/documents")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Helper: seed a bare feature (no docs) for mutation tests
# ---------------------------------------------------------------------------


def _seed_bare_feature(db: Path, slug: str, status: str = "available") -> None:
    conn = connect(db)
    now = "2024-01-01T00:00:00+00:00"
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
    pid = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
    conn.execute(
        "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, slug, status, now, now),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# claim handler
# ---------------------------------------------------------------------------


def test_claim_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/api/projects/proj/features/feat/claim", json={"owner": "Alice"})
    assert resp.status_code == 503


def test_claim_200_transitions_to_in_progress(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/claim",
            json={"owner": "Alice"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "in_progress"
    assert data["changed"] is True


def test_claim_200_noop_when_already_in_progress(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="in_progress")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/claim",
            json={"owner": "Alice"},
        )
    assert resp.status_code == 200
    assert resp.json()["changed"] is False


def test_claim_404_missing_feature(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/no-such/claim",
            json={"owner": "Alice"},
        )
    assert resp.status_code == 404


def test_claim_409_invalid_transition(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="done")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/claim",
            json={"owner": "Alice"},
        )
    assert resp.status_code == 409


def test_claim_400_missing_owner(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/claim", json={})
    assert resp.status_code == 400
    assert "owner" in resp.json()["error"]


def test_claim_400_empty_owner(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/claim",
            json={"owner": "  "},
        )
    assert resp.status_code == 400


def test_claim_400_non_string_owner(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/claim",
            json={"owner": 42},
        )
    assert resp.status_code == 400


def test_claim_400_non_dict_body(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/claim", json=[])
    assert resp.status_code == 400


def test_claim_broadcasts_on_change(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/claim", json={"owner": "Alice"})
    assert resp.status_code == 200
    app.state.broadcaster.broadcast.assert_called_once()


def test_claim_no_broadcast_on_noop(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="in_progress")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/claim", json={"owner": "Alice"})
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    app.state.broadcaster.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# ship handler
# ---------------------------------------------------------------------------


def test_ship_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/api/projects/proj/features/feat/ship", json={})
    assert resp.status_code == 503


def test_ship_200_transitions_to_done(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="in_progress")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/ship",
            json={"outcome": "shipped it"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["changed"] is True


def test_ship_200_noop_when_already_done(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="done")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/ship", json={})
    assert resp.status_code == 200
    assert resp.json()["changed"] is False


def test_ship_404_missing_feature(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/no-such/ship", json={})
    assert resp.status_code == 404


def test_ship_200_backfill_available_to_done(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/ship", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["changed"] is True


def test_ship_409_invalid_transition_from_parked(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="parked")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/ship", json={})
    assert resp.status_code == 409


def test_ship_400_non_string_outcome(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="in_progress")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/ship",
            json={"outcome": 99},
        )
    assert resp.status_code == 400


def test_ship_400_non_dict_body(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="in_progress")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/ship", json=[])
    assert resp.status_code == 400


def test_ship_broadcasts_on_change(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="in_progress")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/ship", json={})
    assert resp.status_code == 200
    app.state.broadcaster.broadcast.assert_called_once()


def test_ship_no_broadcast_on_noop(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="done")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/ship", json={})
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    app.state.broadcaster.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# release handler
# ---------------------------------------------------------------------------


def test_release_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/api/projects/proj/features/feat/release")
    assert resp.status_code == 503


def test_release_200_transitions_to_available(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="in_progress")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/release")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "available"
    assert data["changed"] is True


def test_release_200_noop_when_already_available(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/release")
    assert resp.status_code == 200
    assert resp.json()["changed"] is False


def test_release_404_missing_feature(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/no-such/release")
    assert resp.status_code == 404


def test_release_409_invalid_transition_from_done(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="done")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/release")
    assert resp.status_code == 409


def test_release_broadcasts_on_change(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="in_progress")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/release")
    assert resp.status_code == 200
    app.state.broadcaster.broadcast.assert_called_once()


def test_release_no_broadcast_on_noop(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/release")
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    app.state.broadcaster.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# park handler
# ---------------------------------------------------------------------------


def test_park_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/api/projects/proj/features/feat/park")
    assert resp.status_code == 503


def test_park_200_transitions_to_parked(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/park")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "parked"
    assert data["changed"] is True


def test_park_200_noop_when_already_parked(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="parked")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/park")
    assert resp.status_code == 200
    assert resp.json()["changed"] is False


def test_park_404_missing_feature(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/no-such/park")
    assert resp.status_code == 404


def test_park_409_invalid_transition_from_done(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="done")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/park")
    assert resp.status_code == 409


def test_park_broadcasts_on_change(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/park")
    assert resp.status_code == 200
    app.state.broadcaster.broadcast.assert_called_once()


def test_park_no_broadcast_on_noop(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="parked")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/park")
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    app.state.broadcaster.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# drop handler
# ---------------------------------------------------------------------------


def test_drop_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/api/projects/proj/features/feat/drop")
    assert resp.status_code == 503


def test_drop_200_transitions_to_archived(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/drop")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "archived"
    assert data["changed"] is True


def test_drop_200_empty_body_tolerated(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/drop", content=b"")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"


def test_drop_200_json_object_body_tolerated(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/drop", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"


def test_drop_200_noop_when_already_archived(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="archived")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/drop")
    assert resp.status_code == 200
    assert resp.json()["changed"] is False


def test_drop_404_missing_feature(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/no-such/drop")
    assert resp.status_code == 404


def test_drop_409_invalid_transition_from_done(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="done")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/drop")
    assert resp.status_code == 409


def test_drop_400_non_object_body(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/drop", json=[])
    assert resp.status_code == 400


def test_drop_400_invalid_json_body(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/drop",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400


def test_drop_broadcasts_on_change(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/drop")
    assert resp.status_code == 200
    app.state.broadcaster.broadcast.assert_called_once()


def test_drop_no_broadcast_on_noop(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="archived")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/drop")
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    app.state.broadcaster.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# note handler
# ---------------------------------------------------------------------------


def _seed_bare_feature_with_notes(db: Path, slug: str, notes: str | None) -> None:
    conn = connect(db)
    now = "2024-01-01T00:00:00+00:00"
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
    pid = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
    conn.execute(
        "INSERT INTO features (project_id, slug, status, notes, created_at, updated_at) "
        "VALUES (?, ?, 'available', ?, ?, ?)",
        (pid, slug, notes, now, now),
    )
    conn.commit()
    conn.close()


def test_note_update_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/api/projects/proj/features/feat/note", json={"notes": "x"})
    assert resp.status_code == 503


def test_note_update_200_changes_note(temp_db: Path) -> None:
    _seed_bare_feature_with_notes(temp_db, "feat", "old note")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/note",
            json={"notes": "new note"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "proj"
    assert data["slug"] == "feat"
    assert data["status"] == "available"
    assert data["changed"] is True


def test_note_update_404_missing_feature(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/no-such/note",
            json={"notes": "x"},
        )
    assert resp.status_code == 404


def test_note_update_400_missing_notes_key(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/note", json={})
    assert resp.status_code == 400
    assert "notes" in resp.json()["error"]


def test_note_update_400_non_string_notes(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/note", json={"notes": 5})
    assert resp.status_code == 400


def test_note_update_400_non_object_body(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/note", json=[])
    assert resp.status_code == 400


def test_note_update_broadcasts_on_change(temp_db: Path) -> None:
    _seed_bare_feature_with_notes(temp_db, "feat", "old")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/note", json={"notes": "new"})
    assert resp.status_code == 200
    app.state.broadcaster.broadcast.assert_called_once()


def test_note_update_no_broadcast_on_noop(temp_db: Path) -> None:
    _seed_bare_feature_with_notes(temp_db, "feat", "same")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/feat/note", json={"notes": "same"})
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    app.state.broadcaster.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 0: notices in listing responses
# ---------------------------------------------------------------------------


def test_list_projects_includes_notices(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert "notices" in data
    assert isinstance(data["notices"], list)
    assert len(data["notices"]) > 0


def test_list_features_includes_notices(temp_db: Path) -> None:
    _seed(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/proj-a/features")
    assert resp.status_code == 200
    data = resp.json()
    assert "notices" in data
    assert isinstance(data["notices"], list)
    assert len(data["notices"]) > 0


# ---------------------------------------------------------------------------
# Phase 3: POST /api/projects/{p} — create_project_handler
# ---------------------------------------------------------------------------


def test_create_project_returns_200(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/my-proj")
    assert resp.status_code == 200
    assert resp.json()["project"] == "my-proj"


def test_create_project_409_on_duplicate(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        resp = client.post("/api/projects/proj")
    assert resp.status_code == 409


def test_create_project_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/api/projects/proj")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Phase 3: GET /api/projects/{p} — get_project_handler
# ---------------------------------------------------------------------------


def test_get_project_returns_fields(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/my-proj")
        resp = client.get("/api/projects/my-proj")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "my-proj"
    assert "repo_path" in data


def test_get_project_404_unknown(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/no-such-proj")
    assert resp.status_code == 404


def test_get_project_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/projects/proj")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Phase 1: POST /api/projects/{p}/features/{f} — create_feature_handler
# ---------------------------------------------------------------------------


def test_create_feature_returns_200(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        resp = client.post("/api/projects/proj/features/my-feat", json={"notes": "some notes"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "proj"
    assert data["slug"] == "my-feat"
    assert data["status"] == "available"
    assert data["changed"] is True


def test_create_feature_409_on_duplicate(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        client.post("/api/projects/proj/features/my-feat", json={"notes": "x"})
        resp = client.post("/api/projects/proj/features/my-feat", json={"notes": "x"})
    assert resp.status_code == 409


def test_create_feature_404_project_not_found(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/no-such-proj/features/feat", json={})
    assert resp.status_code == 404
    assert "no-such-proj" in resp.json()["error"]
    assert "POST /api/projects/no-such-proj" in resp.json()["error"]


def test_create_feature_notes_optional(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        resp = client.post("/api/projects/proj/features/no-notes", json={})
    assert resp.status_code == 200


def test_create_feature_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/api/projects/proj/features/feat", json={})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Phase 1: GET /api/projects/{p}/features/{f} — get_feature_handler
# ---------------------------------------------------------------------------


def test_get_feature_returns_fields(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        client.post("/api/projects/proj/features/my-feat", json={"notes": "hello"})
        resp = client.get("/api/projects/proj/features/my-feat")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "proj"
    assert data["slug"] == "my-feat"
    assert data["status"] == "available"
    assert data["notes"] == "hello"
    assert "owner" in data


def test_get_feature_404_unknown_feature(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        resp = client.get("/api/projects/proj/features/no-such-feat")
    assert resp.status_code == 404


def test_get_feature_404_unknown_project(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get("/api/projects/no-such-proj/features/any-feat")
    assert resp.status_code == 404


def test_get_feature_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/projects/proj/features/feat")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Phase 2: capture route retired
# ---------------------------------------------------------------------------


def test_capture_route_gone(temp_db: Path) -> None:
    """POST .../capture was removed in Phase 2; the route no longer exists."""
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/capture", json={})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Phase 5: PUT /api/projects/{p}/suggested-order + GET fields
# ---------------------------------------------------------------------------


def test_put_suggested_order_round_trip(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        resp = client.put("/api/projects/proj/suggested-order", json={"text": "feat-b\nfeat-a\n"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "proj"
    assert data["suggested_order"] == "feat-b\nfeat-a\n"


def test_put_suggested_order_overwrites(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        client.put("/api/projects/proj/suggested-order", json={"text": "old"})
        resp = client.put("/api/projects/proj/suggested-order", json={"text": "new"})
    assert resp.status_code == 200
    assert resp.json()["suggested_order"] == "new"


def test_put_suggested_order_null_clears(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        client.put("/api/projects/proj/suggested-order", json={"text": "some order"})
        resp = client.put("/api/projects/proj/suggested-order", json={"text": ""})
    assert resp.status_code == 200
    assert resp.json()["suggested_order"] is None


def test_put_suggested_order_404_unknown_project(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put("/api/projects/no-such/suggested-order", json={"text": "x"})
    assert resp.status_code == 404


def test_put_suggested_order_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.put("/api/projects/proj/suggested-order", json={"text": "x"})
    assert resp.status_code == 503


def test_put_suggested_order_broadcasts_on_change(temp_db: Path) -> None:
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        client.post("/api/projects/proj")
        app.state.broadcaster = MagicMock()
        resp = client.put("/api/projects/proj/suggested-order", json={"text": "feat-a\n"})
    assert resp.status_code == 200
    app.state.broadcaster.broadcast.assert_called_once()


def test_put_suggested_order_no_broadcast_on_noop(temp_db: Path) -> None:
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        client.post("/api/projects/proj")
        client.put("/api/projects/proj/suggested-order", json={"text": "feat-a\n"})
        app.state.broadcaster = MagicMock()
        resp = client.put("/api/projects/proj/suggested-order", json={"text": "feat-a\n"})
    assert resp.status_code == 200
    app.state.broadcaster.broadcast.assert_not_called()


def test_get_project_includes_suggested_order(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        client.put("/api/projects/proj/suggested-order", json={"text": "feat-x\n"})
        resp = client.get("/api/projects/proj")
    assert resp.status_code == 200
    data = resp.json()
    assert data["suggested_order"] == "feat-x\n"


def test_get_project_suggested_order_null_when_unset(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        resp = client.get("/api/projects/proj")
    assert resp.status_code == 200
    assert resp.json()["suggested_order"] is None


def test_list_features_includes_created_at(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        client.post("/api/projects/proj/features/feat-a", json={})
        resp = client.get("/api/projects/proj/features")
    assert resp.status_code == 200
    feats = resp.json()["features"]
    assert len(feats) == 1
    assert "created_at" in feats[0]
    assert feats[0]["created_at"] is not None


# ---------------------------------------------------------------------------
# Phase 6: GET /api/projects/{p}/features?q=...&status=...
# ---------------------------------------------------------------------------


def _setup_filter_project(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/api/projects/proj")
    client.post("/api/projects/proj/features/alpha", json={"notes": "first feature"})
    client.post("/api/projects/proj/features/beta", json={"notes": "second item"})
    client.post("/api/projects/proj/features/gamma", json={"notes": "UPPER notes"})
    # claim alpha so it has in_progress status
    client.post("/api/projects/proj/features/alpha/claim", json={"owner": "alice"})


def test_list_features_no_params_returns_full_list(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        _setup_filter_project(client)
        resp = client.get("/api/projects/proj/features")
    assert resp.status_code == 200
    slugs = {f["slug"] for f in resp.json()["features"]}
    assert slugs == {"alpha", "beta", "gamma"}


def test_list_features_filter_q_matches_slug(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        _setup_filter_project(client)
        resp = client.get("/api/projects/proj/features?q=alp")
    assert resp.status_code == 200
    slugs = [f["slug"] for f in resp.json()["features"]]
    assert slugs == ["alpha"]


def test_list_features_filter_q_matches_notes(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        _setup_filter_project(client)
        resp = client.get("/api/projects/proj/features?q=second")
    assert resp.status_code == 200
    slugs = [f["slug"] for f in resp.json()["features"]]
    assert slugs == ["beta"]


def test_list_features_filter_q_case_insensitive(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        _setup_filter_project(client)
        resp = client.get("/api/projects/proj/features?q=upper")
    assert resp.status_code == 200
    slugs = [f["slug"] for f in resp.json()["features"]]
    assert slugs == ["gamma"]


def test_list_features_filter_status(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        _setup_filter_project(client)
        resp = client.get("/api/projects/proj/features?status=in_progress")
    assert resp.status_code == 200
    feats = resp.json()["features"]
    assert len(feats) == 1
    assert feats[0]["slug"] == "alpha"
    assert feats[0]["status"] == "in_progress"


def test_list_features_filter_q_and_status_combined(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        _setup_filter_project(client)
        resp = client.get("/api/projects/proj/features?q=eta&status=available")
    assert resp.status_code == 200
    slugs = [f["slug"] for f in resp.json()["features"]]
    assert slugs == ["beta"]


def test_list_features_filter_q_treats_underscore_literally(temp_db: Path) -> None:
    # A '_' in q must match a literal underscore, not the LIKE single-char wildcard.
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        client.post("/api/projects/proj/features/lit-underscore", json={"notes": "has a_b token"})
        client.post("/api/projects/proj/features/lit-other", json={"notes": "has axb token"})
        resp = client.get("/api/projects/proj/features?q=a_b")
    assert resp.status_code == 200
    slugs = [f["slug"] for f in resp.json()["features"]]
    assert slugs == ["lit-underscore"]  # not lit-other, which the wildcard would have matched


def test_list_features_filter_q_treats_percent_literally(temp_db: Path) -> None:
    # A '%' in q must match a literal percent, not the LIKE any-run wildcard.
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        client.post("/api/projects/proj/features/lit-percent", json={"notes": "done 50% today"})
        client.post("/api/projects/proj/features/lit-plain", json={"notes": "done fifty today"})
        resp = client.get("/api/projects/proj/features?q=50%25")  # %25 = url-encoded '%'
    assert resp.status_code == 200
    slugs = [f["slug"] for f in resp.json()["features"]]
    assert slugs == ["lit-percent"]


def test_list_features_filter_empty_result_valid(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        _setup_filter_project(client)
        resp = client.get("/api/projects/proj/features?q=no-such-feature")
    assert resp.status_code == 200
    assert resp.json()["features"] == []


def test_list_features_filter_order_preserved(temp_db: Path) -> None:
    """Filtered results still come back in status, slug order."""
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj")
        client.post("/api/projects/proj/features/zebra", json={})
        client.post("/api/projects/proj/features/apple", json={})
        resp = client.get("/api/projects/proj/features?status=available")
    assert resp.status_code == 200
    slugs = [f["slug"] for f in resp.json()["features"]]
    assert slugs == ["apple", "zebra"]
