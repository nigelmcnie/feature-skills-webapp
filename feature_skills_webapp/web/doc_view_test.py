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


def test_doc_shell_200_with_breadcrumbs_and_iframe(temp_db: Path, tmp_path: Path) -> None:
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
    assert f'src="/doc/{doc_id}/raw"' in response.text


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
    # feature crumb should not appear (tracker has no feature)
    assert "feat-a" not in response.text


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
