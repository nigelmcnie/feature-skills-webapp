"""Tests for web/submit.py (Phase 1: PUT /api/documents endpoint)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from starlette.applications import Starlette
from starlette.testclient import TestClient

from feature_skills_webapp.web.app import create_app

_PUT_URL = "/api/documents/proj/feat-a/requirements/1"
_VALID_BODY = {"sections": {"problem": "<p>The problem.</p>"}}


# --- happy path ---


def test_put_happy_path_returns_all_fields(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put(_PUT_URL, json=_VALID_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] is True
    assert data["changed"] is True
    assert data["version_num"] == 1
    assert data["logical_key"] == "proj/feat-a/requirements/1"
    assert "document_id" in data
    assert data["url"] == f"/doc/{data['document_id']}"


def test_put_doc_fetchable_at_url(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put(_PUT_URL, json=_VALID_BODY)
        doc_id = resp.json()["document_id"]
        doc_resp = client.get(f"/doc/{doc_id}")
    assert doc_resp.status_code == 200


def test_put_second_submit_with_change(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.put(_PUT_URL, json=_VALID_BODY)
        resp = client.put(_PUT_URL, json={"sections": {"problem": "<p>Updated.</p>"}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] is False
    assert data["changed"] is True
    assert data["version_num"] == 2


def test_put_identical_resubmit_no_change(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.put(_PUT_URL, json=_VALID_BODY)
        resp = client.put(_PUT_URL, json=_VALID_BODY)
    data = resp.json()
    assert data["changed"] is False
    assert data["version_num"] == 1


def test_put_actor_defaults_to_agent(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put(_PUT_URL, json=_VALID_BODY)
    assert resp.status_code == 200


def test_put_custom_actor(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put(_PUT_URL, json={**_VALID_BODY, "actor": "codex"})
    assert resp.status_code == 200


# --- dry-run ---
# dry_run returns before hitting broadcaster, so no lifespan needed


def test_dry_run_returns_valid_true(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL + "?dry_run=true", json=_VALID_BODY)
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


def test_dry_run_writes_nothing(temp_db: Path) -> None:
    from feature_skills_webapp.storage.db import connect

    client = TestClient(create_app(db_path=temp_db))
    client.put(_PUT_URL + "?dry_run=true", json=_VALID_BODY)

    conn = connect(temp_db)
    row = conn.execute(
        "SELECT id FROM documents WHERE logical_key='proj/feat-a/requirements/1'"
    ).fetchone()
    conn.close()
    assert row is None


def test_dry_run_true_1_value(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL + "?dry_run=1", json=_VALID_BODY)
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


# --- 400 errors ---
# These all return before hitting the broadcaster


def test_put_400_bad_json(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL, content=b"not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_put_400_body_not_dict(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL, json=["not", "a", "dict"])
    assert resp.status_code == 400


def test_put_400_unknown_section_key(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL, json={"sections": {"made-up-key": "<p>x</p>"}})
    assert resp.status_code == 400
    assert "unknown section key" in resp.json()["error"]


def test_put_400_non_writable_features_type(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put("/api/documents/proj/feat-a/features/1", json={"sections": {}})
    assert resp.status_code == 400
    assert "not writable" in resp.json()["error"]


def test_put_400_feature_dash_maps_to_none(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put("/api/documents/proj/-/requirements/1", json=_VALID_BODY)
    assert resp.status_code == 400
    assert "feature must be specified" in resp.json()["error"]


def test_put_400_instance_2_for_non_feedback(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put("/api/documents/proj/feat-a/requirements/2", json=_VALID_BODY)
    assert resp.status_code == 400
    assert "instance must be 1" in resp.json()["error"]


# --- 503 ---


def test_put_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.put(_PUT_URL, json=_VALID_BODY)
    assert resp.status_code == 503
    assert "db not configured" in resp.json()["error"]


# --- broadcast ---


def test_put_broadcasts(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        app = cast(Starlette, client.app)
        q = app.state.broadcaster.register()
        client.put(_PUT_URL, json=_VALID_BODY)
        assert not q.empty()
        app.state.broadcaster.unregister(q)
