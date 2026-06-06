"""Unit tests for storage/walker.py."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from feature_skills_webapp.storage.db import connect, migrate
from feature_skills_webapp.storage.walker import (
    DocIdentity,
    ParsedDoc,
    identity_for,
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
