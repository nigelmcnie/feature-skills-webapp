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
    assert resp.json() == {"projects": []}


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
# capture handler
# ---------------------------------------------------------------------------


def test_capture_503_no_db() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/api/projects/proj/features/feat/capture", json={})
    assert resp.status_code == 503


def test_capture_200_creates_feature(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/new-feat/capture",
            json={"notes": "hello"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project"] == "proj"
    assert data["slug"] == "new-feat"
    assert data["status"] == "available"
    assert data["changed"] is True


def test_capture_409_feature_already_exists(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "existing")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/existing/capture", json={})
    assert resp.status_code == 409


def test_capture_400_invalid_json(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/capture",
            content="not-json",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400


def test_capture_400_non_string_notes(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(
            "/api/projects/proj/features/feat/capture",
            json={"notes": 123},
        )
    assert resp.status_code == 400


def test_capture_400_non_dict_body(temp_db: Path) -> None:
    # Valid JSON but not an object — exercises the isinstance(body, dict) guard
    # via a path distinct from the malformed-JSON case above.
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post("/api/projects/proj/features/feat/capture", json=[])
    assert resp.status_code == 400


def test_capture_broadcasts_on_change(temp_db: Path) -> None:
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/new-feat/capture", json={})
    assert resp.status_code == 200
    app.state.broadcaster.broadcast.assert_called_once()


def test_capture_no_broadcast_on_existing(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "existing")
    app = create_app(db_path=temp_db)
    with TestClient(app) as client:
        app.state.broadcaster = MagicMock()
        resp = client.post("/api/projects/proj/features/existing/capture", json={})
    assert resp.status_code == 409
    app.state.broadcaster.broadcast.assert_not_called()


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


def test_ship_409_invalid_transition(temp_db: Path) -> None:
    _seed_bare_feature(temp_db, "feat", status="available")
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
