"""Tests for web/comments.py."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from starlette.applications import Starlette
from starlette.testclient import TestClient

from feature_skills_webapp.storage.db import connect
from feature_skills_webapp.web.app import create_app

HTML_REQUIREMENTS = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="requirements">
<title>Requirements</title>
</head>
<body><p>requirements content</p></body>
</html>
"""


def _walk_docs(db_path: Path, docs_root: Path, *, reconcile: bool = True) -> None:
    from feature_skills_webapp.storage.walker import walk

    conn = connect(db_path)
    try:
        walk(conn, docs_root, reconcile=reconcile)
        conn.commit()
    finally:
        conn.close()


def make_requirements_root(tmp_path: Path) -> tuple[Path, str]:
    """Docs root with a single requirements doc. Returns (docs_root, absolute source_path)."""
    docs_root = tmp_path / "docs"
    feat_dir = docs_root / "proj1" / "feat-a"
    feat_dir.mkdir(parents=True)
    req = feat_dir / "requirements.html"
    req.write_text(HTML_REQUIREMENTS)
    return docs_root, str(req)


def get_doc_id(db_path: Path, source_path: str) -> int:
    conn = connect(db_path)
    row = conn.execute("SELECT id FROM documents WHERE source_path = ?", (source_path,)).fetchone()
    conn.close()
    assert row is not None, f"No document found for {source_path}"
    return int(row["id"])


# --- POST /doc/{id}/comments ---


def test_post_comments_writes_active_rows(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    _walk_docs(temp_db, docs_root)
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = get_doc_id(temp_db, source_path)

        payload = {"comments": [{"excerpt": "some text", "text": "my comment"}]}
        resp = client.post(f"/doc/{doc_id}/comments", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["document_id"] == doc_id
        assert data["comments_written"] == 1

        conn = connect(temp_db)
        rows = conn.execute(
            "SELECT status, excerpt, text FROM comments WHERE document_id = ?", (doc_id,)
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["status"] == "active"
        assert rows[0]["excerpt"] == "some text"
        assert rows[0]["text"] == "my comment"


def test_post_comments_null_excerpt_accepted(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    _walk_docs(temp_db, docs_root)
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = get_doc_id(temp_db, source_path)

        resp = client.post(
            f"/doc/{doc_id}/comments",
            json={"comments": [{"text": "no excerpt"}]},
        )
        assert resp.status_code == 200

        conn = connect(temp_db)
        row = conn.execute(
            "SELECT excerpt FROM comments WHERE document_id = ?", (doc_id,)
        ).fetchone()
        conn.close()
        assert row["excerpt"] is None


def test_repost_replaces_active_set(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    _walk_docs(temp_db, docs_root)
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = get_doc_id(temp_db, source_path)

        client.post(
            f"/doc/{doc_id}/comments",
            json={"comments": [{"text": "first"}, {"text": "second"}]},
        )
        client.post(
            f"/doc/{doc_id}/comments",
            json={"comments": [{"text": "replacement"}]},
        )

        conn = connect(temp_db)
        rows = conn.execute(
            "SELECT text FROM comments WHERE document_id = ? AND status = 'active'", (doc_id,)
        ).fetchall()
        conn.close()
        assert [r["text"] for r in rows] == ["replacement"]


def test_repost_leaves_integrated_rows(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    _walk_docs(temp_db, docs_root)
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = get_doc_id(temp_db, source_path)

        client.post(
            f"/doc/{doc_id}/comments",
            json={"comments": [{"text": "will be integrated"}]},
        )

        # Manually mark the row integrated.
        conn = connect(temp_db)
        conn.execute("UPDATE comments SET status = 'integrated' WHERE document_id = ?", (doc_id,))
        conn.close()

        # Re-submit a fresh set.
        client.post(
            f"/doc/{doc_id}/comments",
            json={"comments": [{"text": "new active"}]},
        )

        conn = connect(temp_db)
        rows = conn.execute(
            "SELECT status, text FROM comments WHERE document_id = ? ORDER BY id", (doc_id,)
        ).fetchall()
        conn.close()
        statuses = [(r["status"], r["text"]) for r in rows]
        assert ("integrated", "will be integrated") in statuses
        assert ("active", "new active") in statuses


def test_events_row_written_on_submit(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    _walk_docs(temp_db, docs_root)
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = get_doc_id(temp_db, source_path)

        client.post(
            f"/doc/{doc_id}/comments",
            json={"comments": [{"text": "a"}, {"text": "b"}]},
        )

        conn = connect(temp_db)
        row = conn.execute(
            "SELECT event_type, payload_json FROM events WHERE document_id = ? AND event_type = 'comment_submitted'",
            (doc_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert "2" in row["payload_json"]


def test_post_broadcasts(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    _walk_docs(temp_db, docs_root)
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = get_doc_id(temp_db, source_path)

        app = cast(Starlette, client.app)
        q = app.state.broadcaster.register()
        client.post(f"/doc/{doc_id}/comments", json={"comments": [{"text": "hi"}]})
        assert not q.empty()
        app.state.broadcaster.unregister(q)


def test_post_400_non_string_text(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/doc/1/comments", json={"comments": [{"text": 123}]})
    assert resp.status_code == 400


def test_post_400_non_string_excerpt(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/doc/1/comments", json={"comments": [{"text": "ok", "excerpt": 999}]})
    assert resp.status_code == 400


def test_post_400_missing_text(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/doc/1/comments", json={"comments": [{"excerpt": "e"}]})
    assert resp.status_code == 400


def test_post_400_comments_not_list(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/doc/1/comments", json={"comments": "bad"})
    assert resp.status_code == 400


def test_post_400_body_not_object(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/doc/1/comments",
        content=b'"just a string"',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_post_400_over_size_text(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    _walk_docs(temp_db, docs_root)
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = get_doc_id(temp_db, source_path)
        big = "x" * (1024 * 1024 + 1)
        resp = client.post(f"/doc/{doc_id}/comments", json={"comments": [{"text": big}]})
        assert resp.status_code == 400


def test_post_404_unknown_document(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/doc/99999/comments", json={"comments": []})
    assert resp.status_code == 404


def test_post_malformed_body_returns_400_before_404(temp_db: Path) -> None:
    # Validation runs before the doc-existence check (mirrors synthesis.py), so a
    # malformed body on an unknown doc id is a 400, not a 404.
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/doc/99999/comments", json={"comments": "not a list"})
    assert resp.status_code == 400


def test_post_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/doc/1/comments", json={"comments": []})
    assert resp.status_code == 503
