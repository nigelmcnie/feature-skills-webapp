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

FEATURES_HTML_MULTI = """\
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
        <td class="feature-name">feat-active</td>
        <td class="feature-owner">Alice</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="available">
  <table class="features">
    <tbody>
      <tr>
        <td class="feature-name">feat-available</td>
        <td class="feature-owner"></td>
      </tr>
    </tbody>
  </table>
</section>
<section id="done">
  <table class="features">
    <tbody>
      <tr>
        <td class="feature-name">feat-done</td>
        <td class="feature-owner">Bob</td>
      </tr>
    </tbody>
  </table>
</section>
</body>
</html>
"""

FEATURES_HTML_AVAILABLE_ONLY = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="features">
<title>proj1 — Features</title>
</head>
<body>
<section id="available">
  <table class="features">
    <tbody>
      <tr>
        <td class="feature-name">feat-a</td>
        <td class="feature-owner"></td>
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


def make_docs_root_multi(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-active").mkdir(parents=True)
    (docs_root / "proj1" / "feat-available").mkdir(parents=True)
    (docs_root / "proj1" / "feat-done").mkdir(parents=True)
    for feat in ("feat-active", "feat-available", "feat-done"):
        (docs_root / "proj1" / feat / "context.html").write_text(
            HTML_TEMPLATE.format(doc_type="context", title=f"{feat} context")
        )
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML_MULTI)
    return docs_root


def make_docs_root_with_tracker(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(
        HTML_TEMPLATE.format(doc_type="context", title="feat-a context")
    )
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML_AVAILABLE_ONLY)
    return docs_root


def make_docs_root_available_only(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(
        HTML_TEMPLATE.format(doc_type="context", title="feat-a context")
    )
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML_AVAILABLE_ONLY)
    return docs_root


# ---- 404 / 503 ----


def test_project_page_unknown_returns_404(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_available_only(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/no-such")
    assert resp.status_code == 404


def test_project_page_503_when_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/project/proj1")
    assert resp.status_code == 503


# ---- status grouping ----


def test_project_page_features_grouped_by_status(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_multi(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        # Set up tracker state via API before discover so the walker's INSERT OR IGNORE
        # does not overwrite these statuses.
        client.post("/api/projects/proj1/features/feat-active/capture", json={"notes": ""})
        client.post("/api/projects/proj1/features/feat-active/claim", json={"owner": "Alice"})
        client.post("/api/projects/proj1/features/feat-available/capture", json={"notes": ""})
        client.post("/api/projects/proj1/features/feat-done/capture", json={"notes": ""})
        client.post("/api/projects/proj1/features/feat-done/claim", json={"owner": "Bob"})
        client.post("/api/projects/proj1/features/feat-done/ship", json={"outcome": "Shipped."})
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/proj1")
    assert resp.status_code == 200
    assert "In progress" in resp.text
    assert "Available" in resp.text
    assert "Done" in resp.text
    assert "feat-active" in resp.text
    assert "feat-available" in resp.text
    assert "feat-done" in resp.text
    # in_progress appears before available, available before done
    assert resp.text.index("feat-active") < resp.text.index("feat-available")
    assert resp.text.index("feat-available") < resp.text.index("feat-done")


def test_project_page_feature_links_to_feature_page(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_available_only(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/proj1")
    assert resp.status_code == 200
    assert 'href="/project/proj1/feature/feat-a"' in resp.text


def test_project_page_available_only_renders_list(temp_db: Path, tmp_path: Path) -> None:
    """An available-only project still shows the feature list, not an empty state."""
    docs_root = make_docs_root_available_only(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/proj1")
    assert resp.status_code == 200
    assert "Available" in resp.text
    assert "feat-a" in resp.text


# ---- tracker doc link ----


def test_project_page_tracker_doc_linked_when_present(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root_with_tracker(tmp_path)
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        from feature_skills_webapp.storage.db import connect

        conn = connect(temp_db)
        row = conn.execute(
            "SELECT id FROM documents WHERE type='features' AND feature_id IS NULL LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None, "tracker doc not indexed"
        resp = client.get("/project/proj1")
    assert f'href="/doc/{row["id"]}"' in resp.text


def test_project_page_no_tracker_doc_no_tracker_link(temp_db: Path, tmp_path: Path) -> None:
    """A project without a features.html has no tracker link."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(
        HTML_TEMPLATE.format(doc_type="context", title="ctx")
    )
    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/proj1")
    assert resp.status_code == 200
    assert "feature tracker" not in resp.text


# ---- read-state is not stamped by the project page ----


def test_project_page_does_not_stamp_read_state(temp_db: Path, tmp_path: Path) -> None:
    """Listing a project's features must not mark any doc read."""
    docs_root = make_docs_root_with_tracker(tmp_path)
    from feature_skills_webapp.storage.db import connect

    with TestClient(create_app(db_path=temp_db)) as client:
        _walk_docs(temp_db, docs_root)
        resp = client.get("/project/proj1")
        assert resp.status_code == 200
        conn = connect(temp_db)
        count = conn.execute("SELECT COUNT(*) AS n FROM read_state").fetchone()["n"]
        conn.close()
    assert count == 0
