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
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
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
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
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
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
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
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
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
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
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
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
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
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)
        big = "x" * (1024 * 1024 + 1)
        resp = client.post(f"/doc/{doc_id}/comments", json={"comments": [{"text": big}]})
        assert resp.status_code == 400


def test_post_404_unknown_document(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/doc/99999/comments", json={"comments": []})
    assert resp.status_code == 404


def test_post_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/doc/1/comments", json={"comments": []})
    assert resp.status_code == 503


# --- GET /comments ---


def test_get_none_yet_empty_list_submitted_false(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        resp = client.get(f"/comments?path={source_path}")
        assert resp.status_code == 200
        got = resp.json()
        assert got["submitted"] is False
        assert got["comments"] == []
        assert got["doc"] == source_path


def test_get_returns_active_only_in_id_order(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        client.post(
            f"/doc/{doc_id}/comments",
            json={"comments": [{"text": "first"}, {"text": "second"}]},
        )

        # Manually mark first as integrated.
        conn = connect(temp_db)
        first_id = conn.execute(
            "SELECT id FROM comments WHERE document_id = ? ORDER BY id LIMIT 1", (doc_id,)
        ).fetchone()["id"]
        conn.execute("UPDATE comments SET status = 'integrated' WHERE id = ?", (first_id,))
        conn.close()

        resp = client.get(f"/comments?path={source_path}")
        assert resp.status_code == 200
        got = resp.json()
        assert got["submitted"] is True
        assert len(got["comments"]) == 1
        assert got["comments"][0]["text"] == "second"


def test_get_submitted_true_even_when_only_integrated_remain(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        client.post(f"/doc/{doc_id}/comments", json={"comments": [{"text": "integrated"}]})

        conn = connect(temp_db)
        conn.execute("UPDATE comments SET status = 'integrated' WHERE document_id = ?", (doc_id,))
        conn.close()

        resp = client.get(f"/comments?path={source_path}")
        assert resp.status_code == 200
        got = resp.json()
        assert got["submitted"] is True
        assert got["comments"] == []


def test_get_404_unknown_path(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.get("/comments?path=/no/such/path.html")
    assert resp.status_code == 404


def test_get_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/comments?path=/some/path.html")
    assert resp.status_code == 503


# --- POST /comments/integrate ---


def _post_and_get_ids(client: TestClient, doc_id: int, texts: list[str]) -> list[int]:
    """Submit comments and return their DB ids."""
    client.post(
        f"/doc/{doc_id}/comments",
        json={"comments": [{"text": t} for t in texts]},
    )
    resp = client.get(f"/comments?path={_source_path_for(client, doc_id)}")
    return [c["id"] for c in resp.json()["comments"]]


def _source_path_for(client: TestClient, doc_id: int) -> str:
    from starlette.applications import Starlette

    from feature_skills_webapp.storage.db import connect as db_connect

    app = client.app
    assert isinstance(app, Starlette)
    conn = db_connect(app.state.db_path)
    row = conn.execute("SELECT source_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    assert row is not None
    return row["source_path"]


def test_integrate_marks_given_ids(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        ids = _post_and_get_ids(client, doc_id, ["first", "second", "third"])
        resp = client.post(
            "/comments/integrate",
            json={"path": source_path, "ids": [ids[0], ids[2]]},
        )
        assert resp.status_code == 200
        assert resp.json()["integrated"] == 2

        get_resp = client.get(f"/comments?path={source_path}")
        active = [c["text"] for c in get_resp.json()["comments"]]
        assert active == ["second"]


def test_integrate_excludes_integrated_from_active_read(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        ids = _post_and_get_ids(client, doc_id, ["a", "b"])
        client.post("/comments/integrate", json={"path": source_path, "ids": ids})

        get_resp = client.get(f"/comments?path={source_path}")
        got = get_resp.json()
        assert got["submitted"] is True
        assert got["comments"] == []


def test_integrated_survives_later_active_set_replace(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        ids = _post_and_get_ids(client, doc_id, ["old"])
        client.post("/comments/integrate", json={"path": source_path, "ids": ids})

        # Re-submit a new active set.
        client.post(f"/doc/{doc_id}/comments", json={"comments": [{"text": "new"}]})

        conn = connect(temp_db)
        rows = conn.execute(
            "SELECT status, text FROM comments WHERE document_id = ? ORDER BY id", (doc_id,)
        ).fetchall()
        conn.close()
        statuses = [(r["status"], r["text"]) for r in rows]
        assert ("integrated", "old") in statuses
        assert ("active", "new") in statuses


def test_integrate_cross_doc_ids_are_noops(temp_db: Path, tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    feat_dir = docs_root / "proj1" / "feat-a"
    feat_dir.mkdir(parents=True)
    req_html = HTML_REQUIREMENTS.replace('content="requirements"', 'content="requirements"')
    req_path = feat_dir / "requirements.html"
    req_path.write_text(req_html)
    plan_html = req_html.replace('content="requirements"', 'content="plan"').replace(
        "<title>Requirements</title>", "<title>Plan</title>"
    )
    plan_path = feat_dir / "plan.html"
    plan_path.write_text(plan_html)

    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        req_id = get_doc_id(temp_db, str(req_path))
        get_doc_id(temp_db, str(plan_path))  # ensure plan is indexed

        # Post a comment on requirements and get its id.
        req_comment_ids = _post_and_get_ids(client, req_id, ["req comment"])

        # Try to integrate that id via the plan's path — should not mark it.
        resp = client.post(
            "/comments/integrate",
            json={"path": str(plan_path), "ids": req_comment_ids},
        )
        assert resp.status_code == 200
        assert resp.json()["integrated"] == 0

        # The requirements comment remains active.
        get_resp = client.get(f"/comments?path={req_path!s}")
        assert len(get_resp.json()["comments"]) == 1


def test_integrate_events_row_written(temp_db: Path, tmp_path: Path) -> None:
    docs_root, source_path = make_requirements_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = get_doc_id(temp_db, source_path)

        ids = _post_and_get_ids(client, doc_id, ["x"])
        client.post("/comments/integrate", json={"path": source_path, "ids": ids})

        conn = connect(temp_db)
        row = conn.execute(
            "SELECT event_type FROM events WHERE document_id = ? AND event_type = 'comment_integrated'",
            (doc_id,),
        ).fetchone()
        conn.close()
        assert row is not None


def test_integrate_400_non_int_id(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/comments/integrate", json={"path": "/p.html", "ids": ["bad"]})
    assert resp.status_code == 400


def test_integrate_400_ids_not_list(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/comments/integrate", json={"path": "/p.html", "ids": 42})
    assert resp.status_code == 400


def test_integrate_400_missing_path(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/comments/integrate", json={"ids": []})
    assert resp.status_code == 400


def test_integrate_404_unknown_path(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/comments/integrate", json={"path": "/no/such.html", "ids": []})
    assert resp.status_code == 404


def test_integrate_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/comments/integrate", json={"path": "/p.html", "ids": []})
    assert resp.status_code == 503
