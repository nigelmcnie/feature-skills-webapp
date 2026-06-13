from pathlib import Path

from starlette.testclient import TestClient

from feature_skills_webapp.web.app import create_app

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

# HTML with a proper <main class="document"><section> structure for native-render tests.
PLAN_HTML_WITH_SECTIONS = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="plan">
<title>feat-a plan</title>
</head>
<body>
<main class="document">
<section id="overview"><h2>Overview</h2><p>Overview content here.</p></section>
<section id="key-decisions"><h2>Key technical decisions</h2><p>Decisions here.</p></section>
</main>
</body>
</html>
"""

REQUIREMENTS_HTML_WITH_SECTIONS = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="requirements">
<title>feat-a requirements</title>
</head>
<body>
<main class="document">
<section id="problem"><h2>Problem</h2><p>The problem description.</p></section>
<section id="vision"><h2>Vision</h2><p>The vision statement.</p></section>
</main>
</body>
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


def make_docs_root(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "plan.html").write_text(
        HTML_TEMPLATE.format(doc_type="plan", title="feat-a plan")
    )
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(
        HTML_TEMPLATE.format(doc_type="context", title="feat-a context")
    )
    return docs_root


def make_docs_root_with_tracker(tmp_path: Path) -> Path:
    docs_root = make_docs_root(tmp_path)
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML)
    return docs_root


def make_docs_root_with_archived(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    archive_dir = docs_root / "proj1" / "feat-a" / ".feedback-archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "plan.html").write_text(
        HTML_TEMPLATE.format(doc_type="plan", title="feat-a plan archived")
    )
    return docs_root


# ---- shell 200 and breadcrumbs ----


def test_doc_shell_200_with_breadcrumbs(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        doc_id = conn.execute("SELECT id FROM documents WHERE type='plan' LIMIT 1").fetchone()["id"]
        conn.close()
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "proj1" in response.text
    assert "feat-a" in response.text
    assert "Plan" in response.text
    # Native render: no iframe, view source is a link not an embed
    assert "<iframe" not in response.text
    assert f'href="/doc/{doc_id}/raw"' in response.text


# ---- viewing clears unread ----


def test_viewing_shell_clears_unread(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect
        from feature_skills_webapp.storage.read_state import unread_document_ids

        conn = connect(temp_db)
        doc_id = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
        before = unread_document_ids(conn)
        conn.close()
        assert doc_id in before
        client.get(f"/doc/{doc_id}")
        conn = connect(temp_db)
        after = unread_document_ids(conn)
        conn.close()
    assert doc_id not in after


# ---- raw GET does not stamp read-state ----


def test_raw_does_not_stamp_read_state(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect
        from feature_skills_webapp.storage.read_state import unread_document_ids

        conn = connect(temp_db)
        doc_id = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
        conn.close()
        client.get(f"/doc/{doc_id}/raw")
        conn = connect(temp_db)
        after = unread_document_ids(conn)
        conn.close()
    assert doc_id in after


# ---- 404 cases ----


def test_doc_shell_unknown_id_returns_404(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    response = client.get("/doc/99999")
    assert response.status_code == 404


def test_doc_shell_non_numeric_id_returns_404(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    response = client.get("/doc/abc")
    assert response.status_code == 404


def test_doc_raw_unknown_id_returns_404(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    response = client.get("/doc/99999/raw")
    assert response.status_code == 404


# ---- raw serves file body ----


def test_doc_raw_serves_file_html(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        doc_id = conn.execute("SELECT id FROM documents WHERE type='plan' LIMIT 1").fetchone()["id"]
        conn.close()
        response = client.get(f"/doc/{doc_id}/raw")
    assert response.status_code == 200
    assert "MARKER_plan" in response.text


# ---- missing-file unavailable variant ----


def _get_plan_doc_id(temp_db: Path) -> tuple[int, str]:
    """Return (id, source_path) for the plan doc in the test DB."""
    from feature_skills_webapp.storage.db import connect

    conn = connect(temp_db)
    row = conn.execute("SELECT id, source_path FROM documents WHERE type='plan' LIMIT 1").fetchone()
    conn.close()
    assert row is not None
    return row["id"], row["source_path"]


def test_missing_file_shell_shows_unavailable(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id, source_path = _get_plan_doc_id(temp_db)
        # Delete plan.html; context.html remains so seen_paths is non-empty and
        # the reconcile step's NOT IN clause fires correctly.
        Path(source_path).unlink()
        client.post("/admin/discover")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "no longer available" in response.text
    assert "<iframe" not in response.text


def test_missing_file_raw_returns_404(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id, source_path = _get_plan_doc_id(temp_db)
        Path(source_path).unlink()
        client.post("/admin/discover")
        response = client.get(f"/doc/{doc_id}/raw")
    assert response.status_code == 404


# ---- tracker doc crumb ----


def test_tracker_doc_renders_tracker_crumb(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute(
            "SELECT d.id FROM documents d WHERE d.type='features' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None, "tracker doc not indexed"
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert "Tracker" in response.text
    # No feature breadcrumb link — tracker is project-level, no feature crumb
    assert 'href="/project/proj1/feature/' not in response.text


# ---- archived doc label ----


def test_archived_doc_renders_archived_label(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_archived(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute(
            "SELECT d.id FROM documents d WHERE d.status='archived' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None, "archived doc not indexed"
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert "(archived)" in response.text


def test_archived_doc_raw_serves_file(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_archived(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute(
            "SELECT d.id FROM documents d WHERE d.status='archived' LIMIT 1"
        ).fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}/raw")
    assert response.status_code == 200
    assert "MARKER_plan" in response.text


# ---- 503 when db not configured ----


def test_doc_shell_503_when_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    response = client.get("/doc/1")
    assert response.status_code == 503


# ---- index card links ----


def test_index_unread_card_has_doc_link_and_aria_label(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        doc_id = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
        conn.close()
        response = client.get("/")
    assert response.status_code == 200
    assert f'href="/doc/{doc_id}"' in response.text
    assert "aria-label=" in response.text


def test_index_in_progress_card_not_wrapped_in_anchor(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        # mark the unread doc read so only in-progress shows
        client.post("/admin/projects/proj1/mark-read")
        response = client.get("/")
    assert response.status_code == 200
    assert "In progress" in response.text
    # in-progress cards have no document_id, so no anchor to /doc/
    # The page may have /doc/ links from other sections so just check
    # the in_progress section area doesn't have a card-link
    assert 'class="card-link"' not in response.text


# ---- sibling navigation (Phase 2) ----


def make_three_doc_root(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    for doc_type in ("context", "requirements", "plan"):
        (docs_root / "proj1" / "feat-a" / f"{doc_type}.html").write_text(
            HTML_TEMPLATE.format(doc_type=doc_type, title=f"feat-a {doc_type}")
        )
    return docs_root


def _doc_ids_by_type(temp_db: Path) -> dict[str, int]:
    from feature_skills_webapp.storage.db import connect

    conn = connect(temp_db)
    rows = conn.execute("SELECT id, type FROM documents").fetchall()
    conn.close()
    return {r["type"]: r["id"] for r in rows}


def test_sibling_nav_prev_and_next(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_three_doc_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        ids = _doc_ids_by_type(temp_db)
        response = client.get(f"/doc/{ids['requirements']}")
    assert response.status_code == 200
    assert f'href="/doc/{ids["context"]}"' in response.text
    assert f'href="/doc/{ids["plan"]}"' in response.text


def test_sibling_nav_first_has_no_prev(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_three_doc_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        ids = _doc_ids_by_type(temp_db)
        response = client.get(f"/doc/{ids['context']}")
    assert response.status_code == 200
    assert f'href="/doc/{ids["requirements"]}"' in response.text
    # no prev link pointing anywhere before context
    assert "← Context" not in response.text


def test_sibling_nav_last_has_no_next(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_three_doc_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        ids = _doc_ids_by_type(temp_db)
        response = client.get(f"/doc/{ids['plan']}")
    assert response.status_code == 200
    assert f'href="/doc/{ids["requirements"]}"' in response.text
    assert "Plan →" not in response.text


def test_sibling_nav_order_independent_of_insertion(temp_db: Path, tmp_path: Path) -> None:
    # Index plan first, then context — order must still be context → plan.
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    for doc_type in ("plan", "context"):  # reverse of canonical order
        (docs_root / "proj1" / "feat-a" / f"{doc_type}.html").write_text(
            HTML_TEMPLATE.format(doc_type=doc_type, title=f"feat-a {doc_type}")
        )
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        ids = _doc_ids_by_type(temp_db)
        response = client.get(f"/doc/{ids['plan']}")
    assert response.status_code == 200
    # context is before plan in canonical order, so plan has a prev link to context
    assert f'href="/doc/{ids["context"]}"' in response.text
    # plan is last, so no next link
    assert "Plan →" not in response.text


def test_tracker_doc_has_no_sibling_nav(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute("SELECT id FROM documents WHERE type='features' LIMIT 1").fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert '<nav class="sibling-nav"' not in response.text


def test_archived_feature_doc_has_no_sibling_nav(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_archived(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute("SELECT id FROM documents WHERE status='archived' LIMIT 1").fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert '<nav class="sibling-nav"' not in response.text


# ---- is_synthesis / Submit button (synthesis-response-capture phase 4) ----

HTML_FEEDBACK_NO_META = """\
<!DOCTYPE html>
<html><head><title>Feedback</title></head><body><p>feedback content</p></body></html>
"""


def make_docs_root_with_feedback(tmp_path: Path) -> Path:
    """Docs root with context, plan, and an active feedback doc under feat-a."""
    docs_root = make_docs_root(tmp_path)
    (docs_root / "proj1" / "feat-a" / "requirements-feedback-1.html").write_text(
        HTML_FEEDBACK_NO_META
    )
    return docs_root


def make_docs_root_with_archived_feedback(tmp_path: Path) -> Path:
    """Docs root with a feedback doc in .feedback-archive/ (archived status)."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a" / ".feedback-archive").mkdir(parents=True)
    (
        docs_root / "proj1" / "feat-a" / ".feedback-archive" / "requirements-feedback-1.html"
    ).write_text(HTML_FEEDBACK_NO_META)
    return docs_root


def _doc_id_by_type(temp_db: Path, doc_type: str) -> int:
    from feature_skills_webapp.storage.db import connect

    conn = connect(temp_db)
    row = conn.execute("SELECT id FROM documents WHERE type = ? LIMIT 1", (doc_type,)).fetchone()
    conn.close()
    assert row is not None, f"No document of type {doc_type!r}"
    return int(row["id"])


def test_submit_button_shown_for_active_feedback_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="submit-btn"' in response.text
    assert f"/doc/{doc_id}/synthesis-response" in response.text


def test_submit_button_not_shown_for_plan_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="submit-btn"' not in response.text


def test_submit_button_not_shown_for_archived_feedback_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_archived_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="submit-btn"' not in response.text


def test_siblings_omits_feedback_doc(temp_db: Path, tmp_path: Path) -> None:
    """A feedback doc in feat-a does not appear as a sibling in prev/next nav."""
    docs_root = make_docs_root_with_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        # View the context doc — its only sibling should be plan, not feedback
        context_id = _doc_id_by_type(temp_db, "context")
        plan_id = _doc_id_by_type(temp_db, "plan")
        feedback_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{context_id}")
    assert response.status_code == 200
    assert f'href="/doc/{plan_id}"' in response.text
    assert f'href="/doc/{feedback_id}"' not in response.text


# ---- is_commentable / comment Submit button ----


HTML_REQUIREMENTS = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="requirements">
<title>feat-a requirements</title>
</head>
<body>MARKER_requirements</body>
</html>
"""


def make_docs_root_with_requirements(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "requirements.html").write_text(HTML_REQUIREMENTS)
    return docs_root


def test_comment_button_shown_for_active_requirements_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_requirements(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "requirements")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="comment-submit-btn"' in response.text
    assert f"/doc/{doc_id}/comments" in response.text


def test_comment_button_shown_for_active_plan_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="comment-submit-btn"' in response.text
    assert f"/doc/{doc_id}/comments" in response.text


def test_comment_button_not_shown_for_feedback_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="comment-submit-btn"' not in response.text


def test_comment_button_not_shown_for_tracker_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute("SELECT id FROM documents WHERE type='features' LIMIT 1").fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert 'id="comment-submit-btn"' not in response.text


def test_comment_button_not_shown_for_archived_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_archived(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute("SELECT id FROM documents WHERE status='archived' LIMIT 1").fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert 'id="comment-submit-btn"' not in response.text


# ---- feature breadcrumb href (Phase 3) ----


def test_feature_crumb_carries_feature_page_href(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'href="/project/proj1/feature/feat-a"' in response.text


def test_tracker_doc_crumb_has_no_feature_href(temp_db: Path, tmp_path: Path) -> None:
    """The tracker (project-level) doc has no feature crumb, so no feature-page href."""
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute("SELECT id FROM documents WHERE type='features' LIMIT 1").fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert "/project/proj1/feature/" not in response.text


# ---- project breadcrumb href (Phase 4) ----


def test_project_crumb_carries_project_page_href(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'href="/project/proj1"' in response.text


def test_tracker_project_crumb_carries_project_page_href(temp_db: Path, tmp_path: Path) -> None:
    """The tracker doc's project crumb also links to the project page."""
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute("SELECT id FROM documents WHERE type='features' LIMIT 1").fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert 'href="/project/proj1"' in response.text


# ---- native render (server-rendered-docs) ----


def make_docs_root_with_sectioned_plan(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "plan.html").write_text(PLAN_HTML_WITH_SECTIONS)
    return docs_root


def make_docs_root_with_sectioned_requirements(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "requirements.html").write_text(
        REQUIREMENTS_HTML_WITH_SECTIONS
    )
    return docs_root


def test_plan_renders_natively_no_iframe(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_sectioned_plan(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "<iframe" not in response.text
    # Section content renders as actual HTML tags, not escaped
    assert "<h2>Overview</h2>" in response.text
    assert "&lt;h2&gt;" not in response.text


def test_requirements_renders_natively_no_iframe(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_sectioned_requirements(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "requirements")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "<iframe" not in response.text
    assert "<h2>Problem</h2>" in response.text


def test_tracker_doc_renders_natively_no_iframe(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute("SELECT id FROM documents WHERE type='features' LIMIT 1").fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert "<iframe" not in response.text
    # Tracker content is rendered (extract_safe_inner returns the body content)
    assert "in-progress" in response.text


def test_doc_shell_references_static_assets(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_sectioned_plan(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "/static/doc.css" in response.text
    assert "/static/doc.js" in response.text


def test_feedback_doc_still_uses_framed_render(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "<iframe" in response.text
    assert f'src="/doc/{doc_id}/raw"' in response.text


def _insert_doc_no_version(db: Path, doc_type: str = "plan") -> int:
    """Insert an active document directly without recording a version (simulates pre-F1 doc)."""
    from feature_skills_webapp.storage.db import connect

    conn = connect(db)
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES ('nv-proj', 'n')")
    conn.commit()
    proj_id = conn.execute("SELECT id FROM projects WHERE name='nv-proj'").fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO features (project_id, slug, created_at, updated_at) "
        "VALUES (?, 'nv-feat', 'n', 'n')",
        (proj_id,),
    )
    conn.commit()
    feat_id = conn.execute("SELECT id FROM features WHERE slug='nv-feat'").fetchone()["id"]
    cursor = conn.execute(
        "INSERT INTO documents "
        "(project_id, feature_id, type, status, source_path, logical_key, instance, "
        "metadata_json, source_mtime, created_at, updated_at) "
        "VALUES (?, ?, ?, 'active', '/no/such/file.html', ?, 1, '{}', 0, 'n', 'n')",
        (proj_id, feat_id, doc_type, f"nv-proj/nv-feat/{doc_type}/1"),
    )
    conn.commit()
    doc_id = cursor.lastrowid
    conn.close()
    assert doc_id is not None
    return int(doc_id)


def test_raw_fallback_when_no_version_row(temp_db: Path) -> None:
    doc_id = _insert_doc_no_version(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    # Raw-fallback: shows iframe pointing to raw URL
    assert "<iframe" in response.text
    assert f'src="/doc/{doc_id}/raw"' in response.text
    # No static doc assets in raw-fallback mode
    assert "/static/doc.css" not in response.text


def test_comment_prefill_in_native_render(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_sectioned_requirements(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        doc_id = _doc_id_by_type(temp_db, "requirements")
        # Submit two comments
        client.post(
            f"/doc/{doc_id}/comments",
            json={"comments": [{"excerpt": "some text", "text": "My comment here"}]},
        )
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    # Prefill JSON is embedded in the page
    assert "My comment here" in response.text
    assert "__prefillComments" in response.text
