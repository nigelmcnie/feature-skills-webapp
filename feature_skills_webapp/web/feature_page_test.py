from pathlib import Path

from starlette.testclient import TestClient

from feature_skills_webapp.web.app import create_app


def _walk_docs(db_path: Path, docs_root: Path, *, reconcile: bool = True) -> None:
    from feature_skills_webapp.storage.db import connect
    from feature_skills_webapp.storage.walker import walk

    conn = connect(db_path)
    try:
        walk(conn, docs_root, reconcile=reconcile)
        conn.commit()
    finally:
        conn.close()


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="{doc_type}">
<title>{title}</title>
</head>
<body>MARKER_{doc_type}</body>
</html>
"""

FEATURES_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="features">
<title>proj1 — Features</title>
</head>
<body>
<section id="in-progress">
  <table class="features">
    <tbody>
      <tr>
        <td class="feature-name">feat-a</td>
        <td class="feature-owner">Alice</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="done">
  <table class="features">
    <tbody>
      <tr class="empty"><td colspan="2">Nothing done yet.</td></tr>
    </tbody>
  </table>
</section>
</body>
</html>
"""

HTML_FEEDBACK = """\
<!DOCTYPE html>
<html><head><title>Feedback</title></head><body>feedback content</body></html>
"""


def make_docs_root(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    for doc_type in ("context", "requirements", "plan"):
        (docs_root / "proj1" / "feat-a" / f"{doc_type}.html").write_text(
            HTML_TEMPLATE.format(doc_type=doc_type, title=f"feat-a {doc_type}")
        )
    return docs_root


def make_docs_root_with_feedback(tmp_path: Path) -> Path:
    docs_root = make_docs_root(tmp_path)
    (docs_root / "proj1" / "feat-a" / "requirements-feedback-1.html").write_text(HTML_FEEDBACK)
    return docs_root


def make_docs_root_with_archived(tmp_path: Path) -> Path:
    docs_root = make_docs_root(tmp_path)
    archive = docs_root / "proj1" / "feat-a" / ".feedback-archive"
    archive.mkdir(parents=True)
    (archive / "requirements-feedback-1.html").write_text(HTML_FEEDBACK)
    return docs_root


def make_docs_root_no_docs(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    # feature directory exists but has no HTML docs — the feature must be registered via the API
    return docs_root


def _discover_and_get_feature_page(
    temp_db: Path, docs_root: Path, project: str = "proj1", slug: str = "feat-a"
):
    _walk_docs(temp_db, docs_root)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.get(f"/project/{project}/feature/{slug}")
    return resp


# ---- 404 / 503 ----


def test_feature_page_unknown_project_returns_404(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/no-such/feature/feat-a")
    assert resp.status_code == 404


def test_feature_page_unknown_slug_returns_404(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/proj1/feature/no-such")
    assert resp.status_code == 404


def test_feature_page_503_when_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/project/proj1/feature/feat-a")
    assert resp.status_code == 503


# ---- primary docs in type order ----


def test_feature_page_primary_docs_render_in_type_order(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    resp = _discover_and_get_feature_page(temp_db, docs_root)
    assert resp.status_code == 200
    # all three primary doc types appear
    assert "Context" in resp.text
    assert "Requirements" in resp.text
    assert "Plan" in resp.text
    # context appears before requirements, requirements before plan
    assert resp.text.index("Context") < resp.text.index("Requirements")
    assert resp.text.index("Requirements") < resp.text.index("Plan")


def test_feature_page_bespoke_doc_listed_ranked_last(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    (docs_root / "proj1" / "feat-a" / "vision.html").write_text(
        HTML_TEMPLATE.format(doc_type="vision", title="feat-a vision")
    )
    resp = _discover_and_get_feature_page(temp_db, docs_root)
    assert resp.status_code == 200
    assert "Vision" in resp.text
    # bespoke type sorts after the three known types (doc_type_rank puts unknowns last)
    assert resp.text.index("Plan") < resp.text.index("Vision")


def test_feature_page_primary_doc_links_to_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        doc_id = conn.execute("SELECT id FROM documents WHERE type='plan' LIMIT 1").fetchone()["id"]
        conn.close()
        resp = client.get("/project/proj1/feature/feat-a")
    assert f'href="/doc/{doc_id}"' in resp.text


# ---- feedback section ----


def test_feature_page_unanswered_feedback_badged_awaiting(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_feedback(tmp_path)
    resp = _discover_and_get_feature_page(temp_db, docs_root)
    assert resp.status_code == 200
    assert "Awaiting your input" in resp.text


def test_feature_page_answered_feedback_not_badged_awaiting(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        doc_id = conn.execute(
            "SELECT id FROM documents WHERE type='requirements-feedback' LIMIT 1"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO synthesis_responses (document_id, item_num, response, routine_flag, updated_at) "
            "VALUES (?, 1, 'answer', NULL, '2020-06-01T00:00:00+00:00')",
            (doc_id,),
        )
        conn.commit()
        conn.close()
        resp = client.get("/project/proj1/feature/feat-a")
    assert resp.status_code == 200
    assert "Awaiting your input" not in resp.text


# ---- archived section ----


def test_feature_page_archived_in_own_subsection(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_archived(tmp_path)
    resp = _discover_and_get_feature_page(temp_db, docs_root)
    assert resp.status_code == 200
    assert "Archived" in resp.text


# ---- no docs ----


def test_feature_page_no_docs_renders_empty_hint(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_no_docs(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        # No HTML docs in the feature dir, so the walker won't create the feature row;
        # create it explicitly via the tracker API.
        client.post("/api/projects/proj1")
        client.post("/api/projects/proj1/features/feat-a", json={"notes": ""})
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/proj1/feature/feat-a")
    assert resp.status_code == 200
    assert "No documents yet" in resp.text


# ---- read-state is not stamped by the feature page ----


def test_feature_page_does_not_stamp_read_state(temp_db: Path, tmp_path: Path) -> None:
    """Listing a feature's docs must not mark them read — only opening /doc/{id} does."""
    docs_root = make_docs_root(tmp_path)
    from feature_skills_webapp.storage.db import connect

    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/proj1/feature/feat-a")
        assert resp.status_code == 200
        conn = connect(temp_db)
        count = conn.execute("SELECT COUNT(*) AS n FROM read_state").fetchone()["n"]
        conn.close()
    assert count == 0


# ---- parked status ----


def test_feature_page_renders_parked_status(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        client.post("/api/projects/proj1/features/feat-a/park")
        resp = client.get("/project/proj1/feature/feat-a")
    assert resp.status_code == 200
    assert "parked" in resp.text
