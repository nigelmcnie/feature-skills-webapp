"""Tests for web/tracker.py GET listing handlers."""

from __future__ import annotations

from pathlib import Path

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
