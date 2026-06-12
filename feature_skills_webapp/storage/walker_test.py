"""Unit tests for storage/walker.py."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from feature_skills_webapp.storage.db import connect, migrate
from feature_skills_webapp.storage.inbox import humanise_type
from feature_skills_webapp.storage.versions import backfill_logical_keys, current_content
from feature_skills_webapp.storage.walker import (
    DocIdentity,
    ParsedDoc,
    feedback_instance,
    feedback_type,
    identity_for,
    logical_key,
    parse_doc,
    parse_tracker,
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
<body>
<main class="document">
<section id="content"><p>{title}</p></section>
</main>
</body>
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
    backfill_logical_keys(conn)
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


def test_events_carry_payload_json(tmp_path: Path):
    """Each event records a payload_json with the doc's path, type, and feature."""
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    rows = conn.execute("SELECT payload_json FROM events").fetchall()
    assert rows, "expected events"
    for r in rows:
        assert r["payload_json"] is not None
        payload = json.loads(r["payload_json"])
        assert set(payload) == {"path", "type", "feature"}
        assert payload["path"].endswith(".html")

    # A feature-scoped doc carries its feature slug in the payload.
    ctx = conn.execute(
        "SELECT e.payload_json FROM events e JOIN documents d ON e.document_id = d.id "
        "WHERE d.type = 'context'"
    ).fetchone()
    assert json.loads(ctx["payload_json"])["feature"] == "feat-a"


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


# --- Phase 3: features.html / tracker ---

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
        <td class="feature-name">feat-active</td>
        <td class="feature-owner">Alice</td>
        <td class="feature-notes">doing it now</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="available">
  <table class="features">
    <tbody>
      <tr>
        <td class="feature-name">feat-queued</td>
        <td class="feature-notes">ready to start</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="done">
  <table class="features">
    <tbody>
      <tr>
        <td class="feature-name">feat-done</td>
        <td class="feature-outcome">Shipped.</td>
      </tr>
    </tbody>
  </table>
</section>
</body>
</html>
"""

FEATURES_HTML_EMPTY_SECTION = """\
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
      <tr class="empty">
        <td colspan="3">Nothing in progress.</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="available">
  <table class="features">
    <tbody>
      <tr>
        <td class="feature-name">feat-a</td>
        <td class="feature-notes">queued</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="done">
  <table class="features">
    <tbody>
      <tr class="empty">
        <td colspan="2">Nothing done yet.</td>
      </tr>
    </tbody>
  </table>
</section>
</body>
</html>
"""


# --- parse_tracker unit tests ---


def test_parse_tracker_extracts_rows():
    rows = parse_tracker(FEATURES_HTML)
    assert len(rows) == 3
    slugs = {r.slug for r in rows}
    assert slugs == {"feat-active", "feat-queued", "feat-done"}


def test_parse_tracker_statuses():
    rows = parse_tracker(FEATURES_HTML)
    by_slug = {r.slug: r for r in rows}
    assert by_slug["feat-active"].status == "in_progress"
    assert by_slug["feat-queued"].status == "available"
    assert by_slug["feat-done"].status == "done"


def test_parse_tracker_owner_and_notes():
    rows = parse_tracker(FEATURES_HTML)
    by_slug = {r.slug: r for r in rows}
    assert by_slug["feat-active"].owner == "Alice"
    assert by_slug["feat-active"].notes == "doing it now"
    assert by_slug["feat-queued"].owner is None
    assert by_slug["feat-queued"].notes == "ready to start"
    assert by_slug["feat-done"].notes == "Shipped."


def test_parse_tracker_skips_empty_rows():
    rows = parse_tracker(FEATURES_HTML_EMPTY_SECTION)
    assert len(rows) == 1
    assert rows[0].slug == "feat-a"
    assert rows[0].status == "available"


def test_parse_tracker_returns_empty_on_mangled_html():
    assert parse_tracker("<not valid html at all <<<>>>") == []


def test_parse_tracker_returns_empty_on_no_sections():
    assert parse_tracker("<html><body><p>nothing here</p></body></html>") == []


# --- walk Phase 3 integration tests ---


def test_features_html_indexes_as_project_level_doc(tmp_path: Path):
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML)

    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)
    assert summary.created == 1

    doc = conn.execute("SELECT project_id, feature_id, type FROM documents").fetchone()
    assert doc["feature_id"] is None
    assert doc["type"] == "features"

    proj = conn.execute("SELECT name FROM projects WHERE id=?", (doc["project_id"],)).fetchone()
    assert proj["name"] == "proj1"


def test_tracker_populates_feature_status_owner_notes(tmp_path: Path):
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    features = conn.execute(
        "SELECT slug, status, owner, notes FROM features ORDER BY slug"
    ).fetchall()
    by_slug = {r["slug"]: r for r in features}

    assert by_slug["feat-active"]["status"] == "in_progress"
    assert by_slug["feat-active"]["owner"] == "Alice"
    assert by_slug["feat-active"]["notes"] == "doing it now"
    assert by_slug["feat-queued"]["status"] == "available"
    assert by_slug["feat-done"]["status"] == "done"
    assert by_slug["feat-done"]["notes"] == "Shipped."


def test_tracker_backfills_existing_bare_feature(tmp_path: Path):
    """A feature doc already in the DB (via a per-feature walk) gets status back-filled, not duplicated."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-active").mkdir(parents=True)
    (docs_root / "proj1" / "feat-active" / "context.html").write_text(
        make_html("context", "feat-active ctx")
    )
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    # Only one features row for feat-active (not duplicated)
    count = conn.execute("SELECT COUNT(*) AS n FROM features WHERE slug='feat-active'").fetchone()[
        "n"
    ]
    assert count == 1

    feat = conn.execute("SELECT status, owner FROM features WHERE slug='feat-active'").fetchone()
    assert feat["status"] == "in_progress"
    assert feat["owner"] == "Alice"


def test_mangled_tracker_degrades_gracefully(tmp_path: Path):
    """A mangled features.html still indexes the document, just produces no tracker rows."""
    mangled = """\
<!DOCTYPE html>
<html><head>
<meta name="feature-doc-type" content="features">
<title>Mangled</title>
</head><body><p>no tables at all</p></body></html>
"""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    (docs_root / "proj1" / "features.html").write_text(mangled)

    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)

    # Document is still indexed
    assert summary.created == 1
    assert summary.errors == 0
    # No feature rows from tracker (document has no tables)
    assert conn.execute("SELECT COUNT(*) AS n FROM features").fetchone()["n"] == 0


# --- Phase 4: shipped event ---

FEATURES_HTML_AVAILABLE = """\
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
        <td class="feature-name">feat-x</td>
        <td class="feature-notes">ready to start</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="done">
  <table class="features">
    <tbody>
      <tr class="empty">
        <td colspan="2">Nothing done yet.</td>
      </tr>
    </tbody>
  </table>
</section>
</body>
</html>
"""

FEATURES_HTML_DONE = """\
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
      <tr class="empty">
        <td colspan="2">Nothing available.</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="done">
  <table class="features">
    <tbody>
      <tr>
        <td class="feature-name">feat-x</td>
        <td class="feature-outcome">Shipped.</td>
      </tr>
    </tbody>
  </table>
</section>
</body>
</html>
"""


def test_ship_event_emitted_on_done_transition(tmp_path: Path):
    """Walking a tracker that transitions a feature to done inserts one shipped event."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    features_file = docs_root / "proj1" / "features.html"

    conn = temp_conn(tmp_path)
    features_file.write_text(FEATURES_HTML_AVAILABLE)
    walk(conn, docs_root, reconcile=False)

    # No shipped events yet
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type='shipped'").fetchone()["n"]
        == 0
    )

    # Transition feat-x to done
    import time

    time.sleep(0.01)  # ensure mtime changes
    features_file.write_text(FEATURES_HTML_DONE)
    walk(conn, docs_root, reconcile=False)

    shipped = conn.execute("SELECT * FROM events WHERE event_type='shipped'").fetchall()
    assert len(shipped) == 1
    assert shipped[0]["document_id"] is None
    payload = json.loads(shipped[0]["payload_json"])
    assert payload == {"project": "proj1", "slug": "feat-x"}


def test_ship_event_no_duplicate_on_rewalk(tmp_path: Path):
    """Re-walking a tracker with an already-done feature does not produce a second shipped event."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    features_file = docs_root / "proj1" / "features.html"

    conn = temp_conn(tmp_path)
    features_file.write_text(FEATURES_HTML_AVAILABLE)
    walk(conn, docs_root, reconcile=False)

    import time

    time.sleep(0.01)
    features_file.write_text(FEATURES_HTML_DONE)
    walk(conn, docs_root, reconcile=False)

    # Walk again without changing the file
    walk(conn, docs_root, reconcile=False)

    shipped_count = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type='shipped'"
    ).fetchone()["n"]
    assert shipped_count == 1


FEATURES_HTML_IN_PROGRESS = """\
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
        <td class="feature-name">feat-x</td>
        <td class="feature-notes">working on it</td>
      </tr>
    </tbody>
  </table>
</section>
<section id="done">
  <table class="features">
    <tbody>
      <tr class="empty">
        <td colspan="2">Nothing done yet.</td>
      </tr>
    </tbody>
  </table>
</section>
</body>
</html>
"""


def test_ship_event_not_emitted_for_non_done_status(tmp_path: Path):
    """Transitioning to in_progress or available does not emit a shipped event."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    features_file = docs_root / "proj1" / "features.html"

    conn = temp_conn(tmp_path)
    # Start with available, re-walk with in_progress (no done features in either)
    features_file.write_text(FEATURES_HTML_AVAILABLE)
    walk(conn, docs_root, reconcile=False)

    import time

    time.sleep(0.01)
    features_file.write_text(FEATURES_HTML_IN_PROGRESS)
    walk(conn, docs_root, reconcile=False)

    assert (
        conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type='shipped'").fetchone()["n"]
        == 0
    )


# --- WalkSummary.shipped and .changed ---


def test_done_transition_increments_shipped(tmp_path: Path):
    """Walking a tracker with a new done-transition increments summary.shipped."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    features_file = docs_root / "proj1" / "features.html"

    conn = temp_conn(tmp_path)
    features_file.write_text(FEATURES_HTML_AVAILABLE)
    walk(conn, docs_root, reconcile=False)

    time.sleep(0.01)
    features_file.write_text(FEATURES_HTML_DONE)
    summary = walk(conn, docs_root, reconcile=False)

    assert summary.shipped == 1


def test_changed_true_for_created():
    from feature_skills_webapp.storage.walker import WalkSummary

    assert WalkSummary(created=1).changed is True


def test_changed_true_for_shipped():
    from feature_skills_webapp.storage.walker import WalkSummary

    assert WalkSummary(shipped=1).changed is True


def test_changed_false_for_empty_summary():
    from feature_skills_webapp.storage.walker import WalkSummary

    assert WalkSummary().changed is False


def test_changed_false_for_errors_only():
    from feature_skills_webapp.storage.walker import WalkSummary

    assert WalkSummary(errors=5).changed is False


# --- feedback_type ---

HTML_FEEDBACK_NO_META = """\
<!DOCTYPE html>
<html><head><title>Feedback</title></head><body><p>feedback content</p></body></html>
"""


def test_feedback_type_requirements():
    assert feedback_type(Path("proj/feat/requirements-feedback-1.html")) == "requirements-feedback"


def test_feedback_type_plan():
    assert feedback_type(Path("proj/feat/plan-feedback-2.html")) == "plan-feedback"


def test_feedback_type_non_feedback_returns_none():
    assert feedback_type(Path("proj/feat/requirements.html")) is None
    assert feedback_type(Path("proj/feat/context.html")) is None
    assert feedback_type(Path("proj/features.html")) is None


def test_feedback_type_archived():
    assert (
        feedback_type(Path("proj/feat/.feedback-archive/requirements-feedback-1.html"))
        == "requirements-feedback"
    )


def test_feedback_doc_active_indexes_with_synthetic_type(tmp_path: Path):
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "requirements-feedback-1.html").write_text(
        HTML_FEEDBACK_NO_META
    )

    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)

    assert summary.created == 1
    assert summary.errors == 0

    doc = conn.execute("SELECT type, status FROM documents").fetchone()
    assert doc["type"] == "requirements-feedback"
    assert doc["status"] == "active"


def test_feedback_doc_archived_indexes_as_archived(tmp_path: Path):
    docs_root = tmp_path / "docs"
    archive_dir = docs_root / "proj1" / "feat-a" / ".feedback-archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "requirements-feedback-1.html").write_text(HTML_FEEDBACK_NO_META)

    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)

    assert summary.created == 1
    assert summary.errors == 0

    doc = conn.execute("SELECT type, status FROM documents").fetchone()
    assert doc["type"] == "requirements-feedback"
    assert doc["status"] == "archived"


def test_depth4_non_feedback_archive_still_skipped(tmp_path: Path):
    docs_root = tmp_path / "docs"
    deep_dir = docs_root / "proj1" / "feat-a" / "other-subdir"
    deep_dir.mkdir(parents=True)
    (deep_dir / "requirements-feedback-1.html").write_text(HTML_FEEDBACK_NO_META)

    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)

    assert summary.created == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 0


def test_typeless_non_feedback_doc_still_skipped(tmp_path: Path):
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "not-a-feedback.html").write_text(HTML_FEEDBACK_NO_META)

    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)

    assert summary.created == 0
    assert summary.errors == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == 0


def test_humanise_type_feedback():
    assert humanise_type("requirements-feedback") == "Requirements feedback"
    assert humanise_type("plan-feedback") == "Plan feedback"


# --- feedback_instance and logical_key helpers ---


def test_feedback_instance_returns_n():
    assert feedback_instance(Path("proj/feat/requirements-feedback-1.html")) == 1
    assert feedback_instance(Path("proj/feat/plan-feedback-2.html")) == 2
    assert feedback_instance(Path("proj/feat/requirements-feedback-10.html")) == 10


def test_feedback_instance_non_feedback_returns_1():
    assert feedback_instance(Path("proj/feat/requirements.html")) == 1
    assert feedback_instance(Path("proj/feat/context.html")) == 1


def test_logical_key_feature_doc():
    assert logical_key("proj", "feat", "context", 1) == "proj/feat/context/1"


def test_logical_key_project_level():
    assert logical_key("proj", None, "features", 1) == "proj/-/features/1"


def test_logical_key_feedback():
    assert (
        logical_key("proj", "feat", "requirements-feedback", 2)
        == "proj/feat/requirements-feedback/2"
    )


# --- Phase 2: versioning ---

HTML_WITH_MAIN = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="{doc_type}">
<title>{title}</title>
</head>
<body>
<main class="document">
<section id="content"><p>{body}</p></section>
</main>
</body>
</html>
"""

HTML_NO_MAIN = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="context">
<title>No main</title>
</head>
<body><p>content but no main</p></body>
</html>
"""


def make_section_html(doc_type: str, body: str) -> str:
    return HTML_WITH_MAIN.format(doc_type=doc_type, title="Title", body=body)


def test_fresh_walk_seeds_v1_silently(tmp_path: Path):
    """First walk records v1 for each doc; no 'updated' events, only 'created'."""
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)

    summary = walk(conn, docs_root, reconcile=False)

    assert summary.created == 3
    assert summary.updated == 0

    # Each doc should have exactly one version
    doc_ids = [r["id"] for r in conn.execute("SELECT id FROM documents").fetchall()]
    for doc_id in doc_ids:
        ver_count = conn.execute(
            "SELECT COUNT(*) AS n FROM document_versions WHERE document_id=?", (doc_id,)
        ).fetchone()["n"]
        assert ver_count == 1

    # Only 'created' events (no extra events for the seed)
    event_types = {
        r["event_type"] for r in conn.execute("SELECT event_type FROM events").fetchall()
    }
    assert event_types == {"created"}


def test_identical_resave_cuts_no_version(tmp_path: Path):
    """Re-walking an unchanged file cuts no new version and emits no event."""
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)

    walk(conn, docs_root, reconcile=False)
    event_count_before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    ver_count_before = conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"]

    walk(conn, docs_root, reconcile=False)

    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == event_count_before
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"]
        == ver_count_before
    )


def test_changed_content_cuts_new_version(tmp_path: Path):
    """Modifying a file produces exactly one new version and one updated event."""
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)

    walk(conn, docs_root, reconcile=False)

    target = docs_root / "proj1" / "feat-a" / "context.html"
    time.sleep(0.01)
    target.write_text(make_html("context", "Updated Title"))

    summary = walk(conn, docs_root, reconcile=False)
    assert summary.updated == 1

    doc_id = conn.execute(
        "SELECT id FROM documents WHERE source_path LIKE '%context%' AND type='context'"
    ).fetchone()["id"]
    ver_count = conn.execute(
        "SELECT COUNT(*) AS n FROM document_versions WHERE document_id=?", (doc_id,)
    ).fetchone()["n"]
    assert ver_count == 2

    events = conn.execute("SELECT event_type FROM events WHERE document_id=?", (doc_id,)).fetchall()
    event_types = [r["event_type"] for r in events]
    assert "created" in event_types
    assert "updated" in event_types


def test_walk_leaves_content_html_null(tmp_path: Path):
    """F1 must not populate content_html: doc_raw prefers it over disk (doc_view.py),
    so writing it would silently change the render source. Versioned content lives in
    document_versions instead — content_html stays the untouched F2 seam."""
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    rows = conn.execute("SELECT content_html FROM documents").fetchall()
    assert rows, "expected indexed documents"
    assert all(r["content_html"] is None for r in rows)


def test_seed_existing_row_on_first_phase2_walk(tmp_path: Path):
    """A row that already exists but has no version (legacy row) gets v1 seeded silently."""
    conn = temp_conn(tmp_path)
    now = "2025-01-01T00:00:00+00:00"
    conn.execute("INSERT OR IGNORE INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
    project_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO features (project_id, slug, created_at, updated_at) "
        "VALUES (?, 'feat', ?, ?)",
        (project_id, now, now),
    )
    feature_id = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat'", (project_id,)
    ).fetchone()["id"]

    docs_root = tmp_path / "docs"
    (docs_root / "proj" / "feat").mkdir(parents=True)
    html = make_html("context", "ctx")
    (docs_root / "proj" / "feat" / "context.html").write_text(html)

    mtime_dt = datetime.fromtimestamp(
        os.stat(docs_root / "proj" / "feat" / "context.html").st_mtime, tz=UTC
    ).isoformat()

    # Insert a legacy row with matching mtime (so mtime gate would pass) but no version
    conn.execute(
        "INSERT INTO documents "
        "(project_id, feature_id, type, status, source_path, logical_key, instance, "
        "metadata_json, source_mtime, created_at, updated_at) "
        "VALUES (?, ?, 'context', 'active', ?, 'proj/feat/context/1', 1, "
        "'{\"size\": 0}', ?, ?, ?)",
        (
            project_id,
            feature_id,
            str(docs_root / "proj" / "feat" / "context.html"),
            mtime_dt,
            now,
            now,
        ),
    )
    doc_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    # Ensure size matches so only the "no version" condition triggers re-read
    file_size = (docs_root / "proj" / "feat" / "context.html").stat().st_size
    conn.execute(
        "UPDATE documents SET metadata_json=? WHERE id=?",
        (json.dumps({"size": file_size}), doc_id),
    )

    summary = walk(conn, docs_root, reconcile=False)

    # Should seed v1 silently — no event (not 'updated', not anything new except the seed)
    assert summary.updated == 0
    assert summary.created == 0
    ver = conn.execute(
        "SELECT COUNT(*) AS n FROM document_versions WHERE document_id=?", (doc_id,)
    ).fetchone()["n"]
    assert ver == 1
    # No extra events (no 'updated' for seed)
    events = conn.execute("SELECT event_type FROM events WHERE document_id=?", (doc_id,)).fetchall()
    assert len(events) == 0


def test_archival_reconciles_onto_one_row(tmp_path: Path):
    """Moving a doc to .feedback-archive/ updates the existing row (no new row, no missing event)."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    active_path = docs_root / "proj1" / "feat-a" / "requirements-feedback-1.html"
    active_path.write_text(HTML_FEEDBACK_NO_META)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    doc_id_before = conn.execute("SELECT id FROM documents").fetchone()["id"]

    # Move file to archive
    archive_dir = docs_root / "proj1" / "feat-a" / ".feedback-archive"
    archive_dir.mkdir()
    archived_path = archive_dir / "requirements-feedback-1.html"
    active_path.rename(archived_path)

    summary = walk(conn, docs_root, reconcile=True)

    # Same document row — no duplicate
    rows = conn.execute("SELECT id, status FROM documents").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == doc_id_before
    assert rows[0]["status"] == "archived"

    # No 'missing' event (the row's source_path was updated to the archive path before reconcile)
    assert summary.missing == 0
    missing_events = conn.execute("SELECT id FROM events WHERE event_type='missing'").fetchall()
    assert len(missing_events) == 0


def test_reactivation_with_same_content_no_event(tmp_path: Path):
    """A doc that returns with byte-identical content triggers no reactivated event."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    html = make_section_html("context", "fixed body")
    target = docs_root / "proj1" / "feat-a" / "context.html"
    target.write_text(html)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)
    target.unlink()
    walk(conn, docs_root, reconcile=True)

    # Bring back with identical content
    target.write_text(html)
    summary = walk(conn, docs_root, reconcile=False)

    # No reactivated event because content is identical
    assert summary.reactivated == 0
    assert summary.updated == 0


def test_unparsed_counter_for_section_doc_without_main(tmp_path: Path):
    """A context doc without <main class="document"> increments unparsed and still creates the row."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(HTML_NO_MAIN)

    conn = temp_conn(tmp_path)
    summary = walk(conn, docs_root, reconcile=False)

    assert summary.unparsed == 1
    assert summary.created == 1  # doc still indexed

    doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    ver = conn.execute(
        "SELECT content_json FROM document_versions WHERE document_id=?", (doc_id,)
    ).fetchone()
    assert ver is not None
    import json as _json

    data = _json.loads(ver["content_json"])
    assert data["sections"] == []  # empty sections — the sentinel value


def test_logical_key_stored_on_new_doc(tmp_path: Path):
    """New docs have logical_key and instance populated in the documents row."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(make_html("context", "ctx"))

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    doc = conn.execute("SELECT logical_key, instance FROM documents").fetchone()
    assert doc["logical_key"] == "proj1/feat-a/context/1"
    assert doc["instance"] == 1


def test_feedback_doc_logical_key_uses_instance(tmp_path: Path):
    """A feedback doc uses instance N from filename in its logical_key."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "requirements-feedback-3.html").write_text(
        HTML_FEEDBACK_NO_META
    )

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    doc = conn.execute("SELECT logical_key, instance FROM documents").fetchone()
    assert doc["logical_key"] == "proj1/feat-a/requirements-feedback/3"
    assert doc["instance"] == 3


def test_current_version_via_walk(tmp_path: Path):
    """current_content returns the version seeded on the first walk."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    html = make_section_html("context", "original body")
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(html)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    content = current_content(conn, doc_id)
    assert content is not None
    assert content.shape == "sections"
    assert len(content.sections) == 1
    assert "original body" in content.sections[0].body


# --- Phase 3: Tracker dual-representation + cutover proof ---


def test_features_html_version_is_opaque(tmp_path: Path) -> None:
    """features.html is versioned as shape='opaque' (entire HTML), never section-parsed."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    doc_id = conn.execute("SELECT id FROM documents WHERE type='features'").fetchone()["id"]
    content = current_content(conn, doc_id)
    assert content is not None
    assert content.shape == "opaque"
    assert len(content.sections) == 1
    assert content.sections[0].key == ""
    # The opaque body is the full HTML source
    assert "feat-done" in content.sections[0].body


def test_feedback_version_is_opaque(tmp_path: Path) -> None:
    """Feedback docs are always versioned as shape='opaque', never section-parsed."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "requirements-feedback-1.html").write_text(
        HTML_FEEDBACK_NO_META
    )

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    content = current_content(conn, doc_id)
    assert content is not None
    assert content.shape == "opaque"
    assert len(content.sections) == 1
    assert content.sections[0].key == ""


def test_features_html_dual_representation(tmp_path: Path) -> None:
    """features.html is simultaneously opaque-versioned AND row-extracted.

    Phase 2 content versioning and the Phase 3 tracker extraction coexist: a single
    walk populates both document_versions (opaque shape) and the features rows.
    """
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    # Version exists with opaque shape
    doc_id = conn.execute("SELECT id FROM documents WHERE type='features'").fetchone()["id"]
    content = current_content(conn, doc_id)
    assert content is not None
    assert content.shape == "opaque"

    # Tracker rows are still populated
    features = conn.execute("SELECT slug, status FROM features ORDER BY slug").fetchall()
    by_slug = {r["slug"]: r["status"] for r in features}
    assert by_slug["feat-active"] == "in_progress"
    assert by_slug["feat-queued"] == "available"
    assert by_slug["feat-done"] == "done"

    # Shipped event fires for the feature first seen as 'done'
    shipped = conn.execute("SELECT payload_json FROM events WHERE event_type='shipped'").fetchall()
    assert len(shipped) == 1
    assert json.loads(shipped[0]["payload_json"])["slug"] == "feat-done"


def test_features_html_shipped_event_intact_after_content_change(tmp_path: Path) -> None:
    """Opaque versioning doesn't break the shipped event on done-transition."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1").mkdir(parents=True)
    features_file = docs_root / "proj1" / "features.html"
    features_file.write_text(FEATURES_HTML_AVAILABLE)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    assert (
        conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type='shipped'").fetchone()["n"]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"] == 1

    time.sleep(0.01)
    features_file.write_text(FEATURES_HTML_DONE)
    walk(conn, docs_root, reconcile=False)

    # Shipped event fired
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM events WHERE event_type='shipped'").fetchone()["n"]
        == 1
    )
    # New content version recorded (v2)
    ver_count = conn.execute(
        "SELECT COUNT(*) AS n FROM document_versions WHERE document_id=?",
        (conn.execute("SELECT id FROM documents").fetchone()["id"],),
    ).fetchone()["n"]
    assert ver_count == 2


def test_single_change_isolation(tmp_path: Path) -> None:
    """Modifying one doc cuts exactly one new version; all other doc version counts stay at 1."""
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    conn = temp_conn(tmp_path)

    walk(conn, docs_root, reconcile=False)

    # All 3 docs seeded with v1
    assert conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"] == 3

    target = docs_root / "proj1" / "feat-a" / "context.html"
    time.sleep(0.01)
    target.write_text(make_html("context", "Changed"))

    walk(conn, docs_root, reconcile=False)

    # Total versions: 3 original + 1 new = 4
    assert conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"] == 4

    # Only the changed doc has 2 versions; others still at 1
    doc_versions = conn.execute(
        "SELECT d.source_path, COUNT(*) AS vn FROM document_versions dv "
        "JOIN documents d ON dv.document_id = d.id GROUP BY d.id"
    ).fetchall()
    by_path = {r["source_path"]: r["vn"] for r in doc_versions}
    changed_path = str(target)
    for path, ver_num in by_path.items():
        if path == changed_path:
            assert ver_num == 2, f"expected 2 versions for changed doc, got {ver_num}"
        else:
            assert ver_num == 1, f"expected 1 version for unchanged doc {path}, got {ver_num}"


def test_full_corpus_idempotent_reimport(tmp_path: Path) -> None:
    """A corpus with regular + feedback + archived docs: second walk cuts no new versions or events."""
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / ".feedback-archive").mkdir()

    # Regular doc
    (docs_root / "proj1" / "feat-a" / "requirements.html").write_text(
        make_html("requirements", "req doc")
    )
    # Active feedback
    (docs_root / "proj1" / "feat-a" / "requirements-feedback-1.html").write_text(
        HTML_FEEDBACK_NO_META
    )
    # Archived feedback
    (
        docs_root / "proj1" / "feat-a" / ".feedback-archive" / "requirements-feedback-2.html"
    ).write_text(HTML_FEEDBACK_NO_META)
    # Project-level tracker
    (docs_root / "proj1").mkdir(exist_ok=True)
    (docs_root / "proj1" / "features.html").write_text(FEATURES_HTML_EMPTY_SECTION)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=True)

    ver_count_1 = conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"]
    event_count_1 = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    # Second walk — nothing changed
    walk(conn, docs_root, reconcile=True)

    assert (
        conn.execute("SELECT COUNT(*) AS n FROM document_versions").fetchone()["n"] == ver_count_1
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == event_count_1
