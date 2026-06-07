"""Tests for web/synthesis.py."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from starlette.applications import Starlette
from starlette.testclient import TestClient

from feature_skills_webapp.storage.db import connect
from feature_skills_webapp.web.app import create_app

HTML_FEEDBACK = """\
<!DOCTYPE html>
<html><head><title>Feedback</title></head><body><p>feedback content</p></body></html>
"""


def make_feedback_root(tmp_path: Path) -> tuple[Path, str]:
    """Docs root with a single feedback doc. Returns (docs_root, absolute source_path)."""
    docs_root = tmp_path / "docs"
    feat_dir = docs_root / "proj1" / "feat-a"
    feat_dir.mkdir(parents=True)
    feedback = feat_dir / "requirements-feedback-1.html"
    feedback.write_text(HTML_FEEDBACK)
    return docs_root, str(feedback)


def get_doc_id(db_path: Path, source_path: str) -> int:
    conn = connect(db_path)
    row = conn.execute("SELECT id FROM documents WHERE source_path = ?", (source_path,)).fetchone()
    conn.close()
    assert row is not None, f"No document found for {source_path}"
    return int(row["id"])


# --- POST /doc/{id}/synthesis-response ---


def test_post_then_get_round_trip(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_feedback_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        payload = {
            "responses": {"1": "my answer", "2": ""},
            "routine_flags": {"3": "routine note"},
        }
        resp = client.post(f"/doc/{doc_id}/synthesis-response", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["document_id"] == doc_id
        assert data["items_written"] == 3

        get_resp = client.get(f"/synthesis-response?path={source_path}")
        assert get_resp.status_code == 200
        got = get_resp.json()
        assert got["submitted"] is True
        assert got["responses"] == {"1": "my answer", "2": ""}
        assert got["routine_flags"] == {"3": "routine note"}


def test_empty_string_response_stored_and_marks_submitted(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_feedback_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        resp = client.post(
            f"/doc/{doc_id}/synthesis-response",
            json={"responses": {"1": ""}, "routine_flags": {}},
        )
        assert resp.status_code == 200

        got = client.get(f"/synthesis-response?path={source_path}").json()
        assert got["submitted"] is True
        assert got["responses"] == {"1": ""}


def test_repost_replaces_item_set(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_feedback_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        client.post(
            f"/doc/{doc_id}/synthesis-response",
            json={"responses": {"1": "a", "2": "b", "3": "c"}, "routine_flags": {}},
        )
        client.post(
            f"/doc/{doc_id}/synthesis-response",
            json={"responses": {"4": "d"}, "routine_flags": {}},
        )

        conn = connect(temp_db)
        rows = conn.execute(
            "SELECT item_num FROM synthesis_responses WHERE document_id = ?", (doc_id,)
        ).fetchall()
        conn.close()
        assert [r["item_num"] for r in rows] == [4]


def test_post_404_unknown_document_id(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/doc/99999/synthesis-response", json={"responses": {}, "routine_flags": {}})
    assert resp.status_code == 404


def test_post_400_malformed_body_not_dict(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/doc/1/synthesis-response",
        content=b'"just a string"',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_post_400_responses_not_dict(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/doc/1/synthesis-response",
        json={"responses": "not a dict", "routine_flags": {}},
    )
    assert resp.status_code == 400


def test_post_400_routine_flags_not_dict(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/doc/1/synthesis-response",
        json={"responses": {}, "routine_flags": [1, 2, 3]},
    )
    assert resp.status_code == 400


def test_post_400_non_integer_item_key(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/doc/1/synthesis-response",
        json={"responses": {"bad-key": "value"}, "routine_flags": {}},
    )
    assert resp.status_code == 400


def test_post_400_over_size_value(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_feedback_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)
        big = "x" * (1024 * 1024 + 1)
        resp = client.post(
            f"/doc/{doc_id}/synthesis-response",
            json={"responses": {"1": big}, "routine_flags": {}},
        )
        assert resp.status_code == 400


def test_post_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/doc/1/synthesis-response", json={"responses": {}, "routine_flags": {}})
    assert resp.status_code == 503


# --- GET /synthesis-response ---


def test_get_submitted_false_for_unsubmitted_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_feedback_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        resp = client.get(f"/synthesis-response?path={source_path}")
        assert resp.status_code == 200
        got = resp.json()
        assert got["submitted"] is False
        assert got["responses"] == {}
        assert got["routine_flags"] == {}
        assert got["doc"] == source_path


def test_get_404_unknown_path(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.get("/synthesis-response?path=/no/such/path.html")
    assert resp.status_code == 404


def test_get_400_missing_path_param(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.get("/synthesis-response")
    assert resp.status_code == 400


def test_get_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/synthesis-response?path=/some/path.html")
    assert resp.status_code == 503


# --- broadcast ---


def test_post_broadcasts(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_feedback_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        app = cast(Starlette, client.app)
        q = app.state.broadcaster.register()
        client.post(
            f"/doc/{doc_id}/synthesis-response",
            json={"responses": {"1": "hello"}, "routine_flags": {}},
        )
        assert not q.empty()
        app.state.broadcaster.unregister(q)
