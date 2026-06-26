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
<section id="summary"><h2>Summary</h2><p>The summary description.</p></section>
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        doc_id = conn.execute("SELECT id FROM documents WHERE type='plan' LIMIT 1").fetchone()["id"]
        conn.close()
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "proj1" in response.text
    assert "feat-a" in response.text
    assert "Plan" in response.text
    # Native render: no iframe
    assert "<iframe" not in response.text


# ---- viewing clears unread ----


def test_viewing_shell_clears_unread(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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


# ---- 404 cases ----


def test_doc_shell_unknown_id_returns_404(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    response = client.get("/doc/99999")
    assert response.status_code == 404


def test_doc_shell_non_numeric_id_returns_404(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    response = client.get("/doc/abc")
    assert response.status_code == 404


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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id, source_path = _get_plan_doc_id(temp_db)
        # Delete plan.html; context.html remains so seen_paths is non-empty and
        # the reconcile step's NOT IN clause fires correctly.
        Path(source_path).unlink()
        _walk_docs(temp_db, docs_root)
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "no longer available" in response.text
    assert "<iframe" not in response.text


# ---- tracker doc crumb ----


def test_tracker_doc_renders_tracker_crumb(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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


# ---- 503 when db not configured ----


def test_doc_shell_503_when_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    response = client.get("/doc/1")
    assert response.status_code == 503


# ---- index card links ----


def test_index_unread_card_has_doc_link_and_aria_label(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        doc_id = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
        conn.close()
        response = client.get("/")
    assert response.status_code == 200
    assert f'href="/doc/{doc_id}"' in response.text
    assert "aria-label=" in response.text


def test_index_in_progress_card_not_wrapped_in_anchor(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/proj1/features/feat-a/capture", json={"notes": ""})
        client.post("/api/projects/proj1/features/feat-a/claim", json={"owner": "Alice"})
        _walk_docs(temp_db, docs_root)
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        ids = _doc_ids_by_type(temp_db)
        response = client.get(f"/doc/{ids['requirements']}")
    assert response.status_code == 200
    assert f'href="/doc/{ids["context"]}"' in response.text
    assert f'href="/doc/{ids["plan"]}"' in response.text


def test_sibling_nav_first_has_no_prev(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_three_doc_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        ids = _doc_ids_by_type(temp_db)
        response = client.get(f"/doc/{ids['context']}")
    assert response.status_code == 200
    assert f'href="/doc/{ids["requirements"]}"' in response.text
    # no prev link pointing anywhere before context
    assert "← Context" not in response.text


def test_sibling_nav_last_has_no_next(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_three_doc_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        ids = _doc_ids_by_type(temp_db)
        response = client.get(f"/doc/{ids['plan']}")
    assert response.status_code == 200
    # context is before plan in canonical order, so plan has a prev link to context
    assert f'href="/doc/{ids["context"]}"' in response.text
    # plan is last, so no next link
    assert "Plan →" not in response.text


def test_tracker_doc_has_no_sibling_nav(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute("SELECT id FROM documents WHERE type='features' LIMIT 1").fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert '<nav class="sibling-nav"' not in response.text


def test_archived_feature_doc_has_no_sibling_nav(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_archived(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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

# Vocabulary here MUST match what the feedback template, doc.css, and the
# server-rendered doc.html output all use: id-based tier sections,
# article.feedback-item, li.syn-routine-item. _FeedbackParser keys off these,
# so a fixture in any other vocabulary parses to zero items and the page
# renders blank — the regression this fixture guards against.
HTML_FEEDBACK_WITH_TIERS = """\
<!DOCTYPE html>
<html><head><title>Feedback</title></head><body>
<section id="tier-needs-input">
<h2>Needs your input</h2>
<article class="feedback-item" data-item="1">
  <header><span class="item-num">1.</span><h3>Item one title</h3></header>
  <div class="detail"><p>Detail one.</p></div>
  <div class="my-take"><span class="label">My take:</span> Take one.</div>
  <div class="your-thoughts"><textarea data-item="1"></textarea></div>
</article>
</section>
<section id="tier-routine">
<h2>Routine</h2>
<ul class="routine-list">
<li class="syn-routine-item" data-item="5">
  <span class="item-num">5.</span>
  <span class="body">Routine item five.</span>
  <button class="flag-btn" data-item="5">Flag</button>
  <div class="flag-input"><textarea data-item="5"></textarea></div>
</li>
</ul>
</section>
</body></html>
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="synthesis-submit-btn"' in response.text
    assert f"/doc/{doc_id}/synthesis-response" in response.text


def test_submit_button_not_shown_for_plan_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="submit-btn"' not in response.text


def test_submit_button_not_shown_for_archived_feedback_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_archived_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="submit-btn"' not in response.text


def test_siblings_omits_feedback_doc(temp_db: Path, tmp_path: Path) -> None:
    """A feedback doc in feat-a does not appear as a sibling in prev/next nav."""
    docs_root = make_docs_root_with_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="comment-submit-btn"' in response.text
    assert f"/doc/{doc_id}/comments" in response.text


def test_comment_button_shown_for_active_plan_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="comment-submit-btn"' in response.text
    assert f"/doc/{doc_id}/comments" in response.text


def test_comment_button_not_shown_for_feedback_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'id="comment-submit-btn"' not in response.text


def test_comment_button_not_shown_for_tracker_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute("SELECT id FROM documents WHERE type='features' LIMIT 1").fetchone()
        conn.close()
        response = client.get(f"/doc/{row['id']}")
    assert response.status_code == 200
    assert 'id="comment-submit-btn"' not in response.text


def test_comment_button_not_shown_for_archived_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_archived(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'href="/project/proj1/feature/feat-a"' in response.text


def test_tracker_doc_crumb_has_no_feature_href(temp_db: Path, tmp_path: Path) -> None:
    """The tracker (project-level) doc has no feature crumb, so no feature-page href."""
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert 'href="/project/proj1"' in response.text


def test_tracker_project_crumb_carries_project_page_href(temp_db: Path, tmp_path: Path) -> None:
    """The tracker doc's project crumb also links to the project page."""
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "<iframe" not in response.text
    # Section content renders as actual HTML tags, not escaped
    assert "<h2>Overview</h2>" in response.text
    assert "&lt;h2&gt;" not in response.text


def test_requirements_renders_natively_no_iframe(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_sectioned_requirements(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "<iframe" not in response.text
    assert "<h2>Summary</h2>" in response.text


def test_tracker_doc_renders_natively_no_iframe(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "/static/doc.css" in response.text
    assert "/static/doc.js" in response.text


def test_feedback_doc_renders_natively_no_iframe(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_feedback(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "<iframe" not in response.text
    assert 'id="synthesis-submit-btn"' in response.text


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
    # Raw-fallback: shows "not been parsed" message, no iframe
    assert "not been parsed" in response.text
    assert "<iframe" not in response.text
    # No static doc assets in raw-fallback mode
    assert "/static/doc.css" not in response.text


def test_comment_prefill_in_native_render(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_sectioned_requirements(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
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


# ---- Phase 2: native synthesis ----


def _make_feedback_docs_root_with_tiers(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "requirements-feedback-1.html").write_text(
        HTML_FEEDBACK_WITH_TIERS
    )
    return docs_root


def test_feedback_native_renders_feedback_items(temp_db: Path, tmp_path: Path) -> None:
    docs_root = _make_feedback_docs_root_with_tiers(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "<iframe" not in response.text
    assert "Item one title" in response.text
    assert "Routine item five" in response.text
    assert 'data-item="1"' in response.text
    assert 'data-item="5"' in response.text
    assert 'id="synthesis-submit-btn"' in response.text


def test_feedback_native_toc_not_server_rendered(temp_db: Path, tmp_path: Path) -> None:
    # The TOC is built client-side by doc.js buildToc(), which *appends* to
    # #toc-list. If the template also server-renders entries, the Contents menu
    # is duplicated. Assert the server leaves #toc-list empty (no TOC anchors),
    # matching native/diff modes — doc.js is the single source of the TOC.
    docs_root = _make_feedback_docs_root_with_tiers(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert '<ul id="toc-list"></ul>' in response.text
    assert "data-toc-id" not in response.text


def test_feedback_native_prefill_responses(temp_db: Path, tmp_path: Path) -> None:
    docs_root = _make_feedback_docs_root_with_tiers(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        # Submit synthesis responses first
        client.post(
            f"/doc/{doc_id}/synthesis-response",
            json={"responses": {"1": "My stored response"}, "routine_flags": {}},
        )
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "My stored response" in response.text


def test_feedback_native_prefill_routine_flags(temp_db: Path, tmp_path: Path) -> None:
    docs_root = _make_feedback_docs_root_with_tiers(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "requirements-feedback")
        client.post(
            f"/doc/{doc_id}/synthesis-response",
            json={"responses": {}, "routine_flags": {"5": "Disagree with this"}},
        )
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "Disagree with this" in response.text
    # Checkbox should be pre-checked
    assert 'class="flag-check" data-item="5" checked' in response.text


# ---- diff view (?view=diff) ----

_PLAN_V1 = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="plan">
<title>plan v1</title>
</head>
<body>
<main class="document">
<section id="overview"><h2>Overview</h2><p>This is the original overview.</p></section>
<section id="key-decisions"><h2>Key decisions</h2><p>No changes here.</p></section>
</main>
</body>
</html>
"""

_PLAN_V2 = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="plan">
<title>plan v2</title>
</head>
<body>
<main class="document">
<section id="overview"><h2>Overview</h2><p>This is the updated overview.</p></section>
<section id="key-decisions"><h2>Key decisions</h2><p>No changes here.</p></section>
</main>
</body>
</html>
"""

_PLAN_V2_FORMATTING_ONLY = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="plan">
<title>plan v2 formatting only</title>
</head>
<body>
<main class="document">
<section id="overview"><h2>Overview</h2><div>This is the original overview.</div></section>
<section id="key-decisions"><h2>Key decisions</h2><p>No changes here.</p></section>
</main>
</body>
</html>
"""


def _make_plan_root(tmp_path: Path, html: str) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "plan.html").write_text(html)
    return docs_root


def test_diff_view_shows_diff_for_changed_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        client.get(f"/doc/{doc_id}")  # marks as read
        # Update file and re-index
        (tmp_path / "docs" / "proj1" / "feat-a" / "plan.html").write_text(_PLAN_V2)
        _walk_docs(temp_db, docs_root)
        response = client.get(f"/doc/{doc_id}?view=diff")
    assert response.status_code == 200
    assert 'class="diff-changed"' in response.text
    assert "<ins>" in response.text
    assert "<del>" in response.text


def test_diff_view_note_when_no_prior_version(temp_db: Path, tmp_path: Path) -> None:
    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        # Do NOT read the doc first — no prior version
        response = client.get(f"/doc/{doc_id}?view=diff")
    assert response.status_code == 200
    assert "diff-note" in response.text
    assert "No earlier version" in response.text
    # Falls back to native render — the full doc content is still present
    assert "original overview" in response.text


def test_diff_view_note_when_formatting_only(temp_db: Path, tmp_path: Path) -> None:
    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        client.get(f"/doc/{doc_id}")  # marks as read
        (tmp_path / "docs" / "proj1" / "feat-a" / "plan.html").write_text(_PLAN_V2_FORMATTING_ONLY)
        _walk_docs(temp_db, docs_root)
        response = client.get(f"/doc/{doc_id}?view=diff")
    assert response.status_code == 200
    assert "diff-note" in response.text
    assert "No text changes" in response.text


def test_diff_view_fallback_toggle_offers_view_changes(temp_db: Path, tmp_path: Path) -> None:
    # When ?view=diff falls back to native (formatting-only here), the toggle must reflect the
    # resolved mode — offering "View changes", not a misleading "Full view" for a page that is
    # already the full render.
    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        client.get(f"/doc/{doc_id}")  # marks as read
        (tmp_path / "docs" / "proj1" / "feat-a" / "plan.html").write_text(_PLAN_V2_FORMATTING_ONLY)
        _walk_docs(temp_db, docs_root)
        response = client.get(f"/doc/{doc_id}?view=diff")
    assert response.status_code == 200
    assert "View changes" in response.text
    assert "Full view" not in response.text


def test_diff_view_mark_read_stamped(temp_db: Path, tmp_path: Path) -> None:
    from feature_skills_webapp.storage.db import connect
    from feature_skills_webapp.storage.read_state import unread_document_ids

    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        conn = connect(temp_db)
        assert doc_id in unread_document_ids(conn)
        conn.close()
        client.get(f"/doc/{doc_id}?view=diff")
        conn = connect(temp_db)
        after = unread_document_ids(conn)
        conn.close()
    assert doc_id not in after


def test_diff_toggle_shown_for_section_doc(temp_db: Path, tmp_path: Path) -> None:
    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        response = client.get(f"/doc/{doc_id}")
    assert response.status_code == 200
    assert "View changes" in response.text
    assert f'href="/doc/{doc_id}?view=diff"' in response.text


def test_diff_toggle_shows_full_view_when_in_diff_mode(temp_db: Path, tmp_path: Path) -> None:
    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        client.get(f"/doc/{doc_id}")  # marks as read
        (tmp_path / "docs" / "proj1" / "feat-a" / "plan.html").write_text(_PLAN_V2)
        _walk_docs(temp_db, docs_root)
        response = client.get(f"/doc/{doc_id}?view=diff")
    assert response.status_code == 200
    assert "Full view" in response.text
    assert f'href="/doc/{doc_id}"' in response.text


def test_no_iframe_for_doc_with_stored_content(temp_db: Path, tmp_path: Path) -> None:
    """Guard: no doc with stored content (non-raw-fallback) should emit an iframe."""
    docs_root = _make_feedback_docs_root_with_tiers(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        doc_ids = [
            r["id"]
            for r in conn.execute("SELECT id FROM documents WHERE status='active'").fetchall()
        ]
        conn.close()
        for doc_id in doc_ids:
            response = client.get(f"/doc/{doc_id}")
            assert response.status_code == 200
            # Raw-fallback is allowed to use iframe; check for stored content first
            has_content = (
                'id="doc-main"' in response.text or 'id="synthesis-submit-btn"' in response.text
            )
            if has_content:
                assert "<iframe" not in response.text, (
                    f"doc {doc_id} has stored content but emits iframe"
                )


# ---------------------------------------------------------------------------
# extra_css rendering — no chrome bleed, mode boundary, scope-and-keep
# ---------------------------------------------------------------------------

_PUT_URL = "/api/documents/proj/feat-a/requirements/1"
_VALID_SECTIONS = {"sections": {"summary": "<p>The summary.</p>"}}


def _put_doc(client: TestClient, extra_css: str = "") -> int:
    body: dict[str, object] = {**_VALID_SECTIONS}
    if extra_css:
        body["extra_css"] = extra_css
    resp = client.put(_PUT_URL, json=body)
    assert resp.status_code == 200
    return resp.json()["document_id"]


def test_native_render_includes_scoped_style_for_extra_css(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = _put_doc(client, "table { border: 1px solid red }")
        resp = client.get(f"/doc/{doc_id}")
    assert resp.status_code == 200
    assert "@scope (#doc-main)" in resp.text
    assert "table { border: 1px solid red }" in resp.text


def test_native_render_no_scoped_style_when_extra_css_absent(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = _put_doc(client, "")
        resp = client.get(f"/doc/{doc_id}")
    assert resp.status_code == 200
    assert "@scope" not in resp.text


def test_scoped_style_not_in_diff_render(temp_db: Path) -> None:
    # Pin a GENUINE diff: v1 → read (baseline) → v2 with a real textual change,
    # then view=diff. The diff-changed assertion fails loudly if it falls back to
    # native, so the @scope-absent assertion can be unconditional.
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = _put_doc(client, "table { color: blue }")
        client.get(f"/doc/{doc_id}")  # establish the read baseline after v1
        client.put(
            _PUT_URL,
            json={
                "sections": {"summary": "<p>Genuinely changed text.</p>"},
                "extra_css": "table { color: blue }",
            },
        )
        resp = client.get(f"/doc/{doc_id}?view=diff")
    assert resp.status_code == 200
    assert 'class="diff-changed"' in resp.text  # a real diff render, not a native fallback
    assert "@scope" not in resp.text  # scoped style is never injected in diff mode


def test_extra_css_with_stray_brace_rejected_at_write(temp_db: Path) -> None:
    # The chrome-bleed defence is enforced at the write boundary: extra_css with a
    # stray } (which would close @scope early and let a rule target the chrome) is
    # rejected, so it can never be stored or rendered. Asserts the observable
    # outcome (400 + the doc is not created), not the shape of the emitted markup.
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put(
            _PUT_URL,
            json={**_VALID_SECTIONS, "extra_css": "} .crumbs { display:none }"},
        )
        assert resp.status_code == 400
        assert "}" in resp.json()["error"]
        # The rejected write left no document behind.
        assert client.get(_PUT_URL).status_code == 404


# ---------------------------------------------------------------------------
# acked_version advancement and unreviewed banner
# ---------------------------------------------------------------------------


def test_native_view_single_version_advances_acked_version(temp_db: Path, tmp_path: Path) -> None:
    """A plain GET of a single-version doc calls mark_diff_seen → acked_version = 1."""
    from feature_skills_webapp.storage.db import connect
    from feature_skills_webapp.storage.read_state import acked_version

    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        client.get(f"/doc/{doc_id}")

    conn = connect(temp_db)
    av = acked_version(conn, doc_id)
    conn.close()
    assert av == 1


def test_native_view_multi_version_shows_unreviewed_banner(temp_db: Path, tmp_path: Path) -> None:
    """Plain GET after a second version is pushed shows the 'View changes' banner."""
    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        client.get(f"/doc/{doc_id}")  # acked_version advances to 1
        # Push v2
        (tmp_path / "docs" / "proj1" / "feat-a" / "plan.html").write_text(_PLAN_V2)
        _walk_docs(temp_db, docs_root)
        response = client.get(f"/doc/{doc_id}")  # plain view, v2 not acked
    assert response.status_code == 200
    assert "View changes" in response.text
    assert f'href="/doc/{doc_id}?view=diff"' in response.text


def test_diff_view_advances_acked_version_to_latest(temp_db: Path, tmp_path: Path) -> None:
    """A ?view=diff GET calls mark_diff_seen → acked_version advances to the latest version."""
    from feature_skills_webapp.storage.db import connect
    from feature_skills_webapp.storage.read_state import acked_version

    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        client.get(f"/doc/{doc_id}")  # acked_version → 1
        (tmp_path / "docs" / "proj1" / "feat-a" / "plan.html").write_text(_PLAN_V2)
        _walk_docs(temp_db, docs_root)
        client.get(f"/doc/{doc_id}?view=diff")  # acked_version → 2

    conn = connect(temp_db)
    av = acked_version(conn, doc_id)
    conn.close()
    assert av == 2


def test_unreviewed_banner_absent_after_diff_viewed(temp_db: Path, tmp_path: Path) -> None:
    """After viewing the diff, the plain view no longer shows the banner."""
    docs_root = _make_plan_root(tmp_path, _PLAN_V1)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        doc_id = _doc_id_by_type(temp_db, "plan")
        client.get(f"/doc/{doc_id}")  # acked → 1
        (tmp_path / "docs" / "proj1" / "feat-a" / "plan.html").write_text(_PLAN_V2)
        _walk_docs(temp_db, docs_root)
        client.get(f"/doc/{doc_id}?view=diff")  # acked → 2
        response = client.get(f"/doc/{doc_id}")  # plain view, already acked
    assert response.status_code == 200
    # Banner must not appear — acked_version matches latest
    assert "This document changed" not in response.text


def test_scope_and_keep_opaque_doc(tmp_path: Path, temp_db: Path) -> None:
    # An opaque (non-feedback) doc with a <style> block has its CSS gathered and scoped.
    # Must be walker-imported because the API only accepts opaque for *-feedback types.
    # Use an unknown doc type so it's treated as opaque but not synthesis-native.
    docs_root = tmp_path / "docs"
    (docs_root / "proj" / "feat-a").mkdir(parents=True)
    (docs_root / "proj" / "feat-a" / "release-notes.html").write_text(
        "<!DOCTYPE html><html><head>"
        '<meta charset="UTF-8"><meta name="feature-doc-type" content="release-notes">'
        "</head><body><main class='document'>"
        "<style>table { border: 2px solid green }</style>"
        "<p>Release content</p>"
        "</main></body></html>"
    )
    _walk_docs(temp_db, docs_root)
    with TestClient(create_app(db_path=temp_db)) as client:
        # Find the doc id
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute(
            "SELECT id FROM documents WHERE logical_key=?", ("proj/feat-a/release-notes/1",)
        ).fetchone()
        conn.close()
        assert row is not None
        doc_id = row["id"]
        page = client.get(f"/doc/{doc_id}")
    assert page.status_code == 200
    assert "@scope (#doc-main)" in page.text
    assert "table" in page.text
    assert "border: 2px solid green" in page.text
