"""Unit tests for storage/walker.py."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from feature_skills_webapp.storage.db import connect, migrate
from feature_skills_webapp.storage.walker import (
    DocIdentity,
    ParsedDoc,
    identity_for,
    parse_doc,
    walk,
)

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="{doc_type}">
<title>{title}</title>
</head>
<body><p>content</p></body>
</html>
"""

HTML_NO_META = """\
<!DOCTYPE html>
<html><head><title>No meta</title></head><body></body></html>
"""


def make_html(doc_type: str, title: str = "Test Doc") -> str:
    return HTML_TEMPLATE.format(doc_type=doc_type, title=title)


def temp_conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    return conn


# --- identity_for ---


def test_identity_feature_doc():
    rel = Path("myproject/myfeature/context.html")
    ident = identity_for(rel)
    assert ident == DocIdentity(project="myproject", feature="myfeature", archived=False)


def test_identity_archived_doc():
    rel = Path("myproject/myfeature/.feedback-archive/context.html")
    ident = identity_for(rel)
    assert ident == DocIdentity(project="myproject", feature="myfeature", archived=True)


def test_identity_project_level_features_html():
    rel = Path("myproject/features.html")
    ident = identity_for(rel)
    assert ident == DocIdentity(project="myproject", feature=None, archived=False)


def test_identity_skips_bare_file_at_root():
    assert identity_for(Path("bare.html")) is None


def test_identity_skips_dotfile_feature():
    assert identity_for(Path("myproject/.hidden/context.html")) is None


def test_identity_skips_too_deep():
    assert identity_for(Path("p/f/sub/deep.html")) is None


def test_identity_skips_non_features_project_level():
    # Only features.html is recognised as a project-level doc
    assert identity_for(Path("myproject/other.html")) is None


# --- parse_doc ---


def test_parse_doc_extracts_type_and_title(tmp_path: Path):
    p = tmp_path / "context.html"
    p.write_text(make_html("context", "My Context Doc"))
    result = parse_doc(p)
    assert result == ParsedDoc(doc_type="context", title="My Context Doc")


def test_parse_doc_returns_none_without_meta(tmp_path: Path):
    p = tmp_path / "no_meta.html"
    p.write_text(HTML_NO_META)
    assert parse_doc(p) is None


def test_parse_doc_returns_none_for_missing_file(tmp_path: Path):
    assert parse_doc(tmp_path / "nonexistent.html") is None


# --- walk ---


def _make_tree(docs_root: Path) -> None:
    """Create a minimal docs tree under docs_root."""
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(
        make_html("context", "feat-a context")
    )
    (docs_root / "proj1" / "feat-a" / "plan.html").write_text(make_html("plan", "feat-a plan"))
    (docs_root / "proj1" / "feat-b").mkdir(parents=True)
    (docs_root / "proj1" / "feat-b" / "requirements.html").write_text(
        make_html("requirements", "feat-b req")
    )


def test_fresh_walk_indexes_projects_features_documents(tmp_path: Path):
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)

    summary = walk(conn, docs_root, reconcile=False)

    assert summary.created == 3
    assert summary.errors == 0

    projects = conn.execute("SELECT name FROM projects").fetchall()
    assert len(projects) == 1
    assert projects[0]["name"] == "proj1"

    features = conn.execute("SELECT slug FROM features ORDER BY slug").fetchall()
    assert [r["slug"] for r in features] == ["feat-a", "feat-b"]

    docs = conn.execute("SELECT type, status FROM documents ORDER BY type").fetchall()
    types = {r["type"] for r in docs}
    assert types == {"context", "plan", "requirements"}
    assert all(r["status"] == "active" for r in docs)


def test_fresh_walk_emits_events(tmp_path: Path):
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)
    events = conn.execute("SELECT event_type FROM events").fetchall()
    assert all(r["event_type"] == "created" for r in events)
    assert len(events) == 3


def test_second_walk_is_noop(tmp_path: Path):
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)
    event_count_after_first = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    summary = walk(conn, docs_root, reconcile=False)

    assert summary.updated == 0
    assert summary.created == 0
    event_count_after_second = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert event_count_after_second == event_count_after_first


def test_changed_file_triggers_update(tmp_path: Path):
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    # Modify a file — change mtime by writing new content
    target = docs_root / "proj1" / "feat-a" / "context.html"
    time.sleep(0.01)  # ensure mtime changes
    target.write_text(make_html("context", "Updated Title"))

    summary = walk(conn, docs_root, reconcile=False)
    assert summary.updated == 1


def test_reconcile_marks_removed_file_missing(tmp_path: Path):
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    # Remove one file
    (docs_root / "proj1" / "feat-b" / "requirements.html").unlink()

    summary = walk(conn, docs_root, reconcile=True)
    assert summary.missing == 1

    missing = conn.execute(
        "SELECT status FROM documents WHERE source_path LIKE '%requirements%'"
    ).fetchone()
    assert missing["status"] == "missing"

    events = conn.execute("SELECT event_type FROM events WHERE event_type='missing'").fetchall()
    assert len(events) == 1


def test_reconcile_does_not_delete_rows(tmp_path: Path):
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)
    (docs_root / "proj1" / "feat-b" / "requirements.html").unlink()
    walk(conn, docs_root, reconcile=True)
    count = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    assert count == 3  # all rows still present


def test_reactivation_in_place(tmp_path: Path):
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    target = docs_root / "proj1" / "feat-b" / "requirements.html"
    target.unlink()
    walk(conn, docs_root, reconcile=True)

    # Bring it back
    target.write_text(make_html("requirements", "feat-b req back"))
    summary = walk(conn, docs_root, reconcile=False)
    assert summary.reactivated == 1

    doc = conn.execute(
        "SELECT status FROM documents WHERE source_path LIKE '%requirements%'"
    ).fetchone()
    assert doc["status"] == "active"

    # The original doc row count is unchanged (reactivated in place, not duplicated)
    count = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    assert count == 3


def test_archived_doc_indexes_as_archived(tmp_path: Path):
    docs_root = tmp_path / "docs"
    archive_dir = docs_root / "proj1" / "feat-a" / ".feedback-archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "old_context.html").write_text(make_html("context", "archived ctx"))

    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)
    assert summary.created == 1

    doc = conn.execute("SELECT status FROM documents").fetchone()
    assert doc["status"] == "archived"


def test_file_without_meta_tag_is_skipped(tmp_path: Path):
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "bad.html").write_text(HTML_NO_META)

    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)

    assert summary.created == 0
    assert summary.errors == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 0


def test_summary_counts_correct(tmp_path: Path):
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)

    assert summary.created == 3
    assert summary.updated == 0
    assert summary.archived == 0
    assert summary.missing == 0
    assert summary.reactivated == 0
    assert summary.errors == 0


def test_walk_nonexistent_docs_root(tmp_path: Path):
    conn = temp_conn(tmp_path)
    summary = walk(conn, tmp_path / "nonexistent", reconcile=False)
    assert summary.created == 0
    assert summary.errors == 0


def test_no_op_walk_emits_no_events(tmp_path: Path):
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    walk(conn, docs_root, reconcile=False)
    after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert before == after
