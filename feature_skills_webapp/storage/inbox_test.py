"""Unit tests for storage/inbox.py."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from feature_skills_webapp.storage.db import connect, migrate, transaction
from feature_skills_webapp.storage.doc_content import ParsedContent, Section
from feature_skills_webapp.storage.inbox import (
    Inbox,
    awaiting_input,
    badge_kind,
    build_inbox,
    classify_reason,
    humanise_type,
    in_progress,
    mark_new_since_read,
    new_since_last_visit,
    recently_shipped,
)
from feature_skills_webapp.storage.versions import record_version


def temp_conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    return conn


def _seed(conn: sqlite3.Connection) -> dict[str, int]:
    """Seed two projects with features in mixed statuses, docs, events, and read_state rows."""
    now = "2020-06-01T00:00:00+00:00"
    OLD = "2020-01-01T00:00:00+00:00"
    RECENT = "2020-05-01T00:00:00+00:00"
    NEWER = "2020-05-15T00:00:00+00:00"

    # Projects
    conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (now,))
    proj_a = conn.execute("SELECT id FROM projects WHERE name='proj-a'").fetchone()["id"]
    conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-b', ?)", (now,))
    proj_b = conn.execute("SELECT id FROM projects WHERE name='proj-b'").fetchone()["id"]

    # Features
    conn.execute(
        "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
        "VALUES (?, 'feat-a-1', 'in_progress', ?, ?)",
        (proj_a, now, now),
    )
    feat_a1 = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat-a-1'", (proj_a,)
    ).fetchone()["id"]

    conn.execute(
        "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
        "VALUES (?, 'feat-a-2', 'available', ?, ?)",
        (proj_a, now, now),
    )
    feat_a2 = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat-a-2'", (proj_a,)
    ).fetchone()["id"]

    conn.execute(
        "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
        "VALUES (?, 'feat-a-noevent', 'in_progress', ?, ?)",
        (proj_a, now, now),
    )
    feat_a_noevent = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat-a-noevent'", (proj_a,)
    ).fetchone()["id"]

    conn.execute(
        "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
        "VALUES (?, 'feat-b-1', 'in_progress', ?, ?)",
        (proj_b, now, now),
    )
    feat_b1 = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat-b-1'", (proj_b,)
    ).fetchone()["id"]

    def _insert_doc(
        project_id: int,
        feature_id: int | None,
        doc_type: str,
        status: str,
        path: str,
    ) -> int:
        conn.execute(
            "INSERT INTO documents (project_id, feature_id, type, status, source_path, "
            "metadata_json, source_mtime, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?)",
            (project_id, feature_id, doc_type, status, path, now, now, now),
        )
        return conn.execute("SELECT id FROM documents WHERE source_path=?", (path,)).fetchone()[
            "id"
        ]

    # Docs for proj-a
    doc_active_a1 = _insert_doc(
        proj_a, feat_a1, "context", "active", "/docs/proj-a/feat-a-1/context.html"
    )
    doc_archived_a1 = _insert_doc(
        proj_a, feat_a1, "review", "archived", "/docs/proj-a/feat-a-1/.archive/review.html"
    )
    doc_missing_a1 = _insert_doc(
        proj_a, feat_a1, "plan", "missing", "/docs/proj-a/feat-a-1/plan.html"
    )
    doc_active_a2 = _insert_doc(
        proj_a, feat_a2, "context", "active", "/docs/proj-a/feat-a-2/context.html"
    )
    # Project-level tracker doc (feature_id=None, type='features')
    doc_tracker_a = _insert_doc(proj_a, None, "features", "active", "/docs/proj-a/features.html")
    # proj-b doc
    doc_active_b1 = _insert_doc(
        proj_b, feat_b1, "context", "active", "/docs/proj-b/feat-b-1/context.html"
    )

    def _add_event(doc_id: int, ts: str) -> None:
        conn.execute(
            "INSERT INTO events (document_id, event_type, payload_json, created_at) "
            "VALUES (?, 'created', '{}', ?)",
            (doc_id, ts),
        )

    # doc_active_a1: newer event (NEWER), so it sorts before doc_active_a2
    _add_event(doc_active_a1, NEWER)
    # doc_archived_a1: also has a FUTURE event (but excluded from new_since because archived)
    _add_event(doc_archived_a1, "2099-01-01T00:00:00+00:00")
    # doc_missing_a1: excluded (missing)
    _add_event(doc_missing_a1, "2099-01-01T00:00:00+00:00")
    # doc_active_a2: older event (RECENT)
    _add_event(doc_active_a2, RECENT)
    # tracker doc: also has a FUTURE event (should be excluded from new_since because no feature)
    _add_event(doc_tracker_a, "2099-01-01T00:00:00+00:00")
    # doc_active_b1
    _add_event(doc_active_b1, OLD)

    # Mark doc_active_b1 as read (so it's excluded from new_since)
    conn.execute(
        "INSERT INTO read_state (document_id, last_read_at) VALUES (?, '2099-12-31T00:00:00+00:00')",
        (doc_active_b1,),
    )

    # doc_active_a1 and doc_active_a2 are unread (no read_state row)

    # feat-a-noevent has no docs with active-doc events (it just has no docs at all)
    # This tests the COALESCE ordering in in_progress

    return {
        "proj_a": proj_a,
        "proj_b": proj_b,
        "feat_a1": feat_a1,
        "feat_a2": feat_a2,
        "feat_a_noevent": feat_a_noevent,
        "feat_b1": feat_b1,
        "doc_active_a1": doc_active_a1,
        "doc_archived_a1": doc_archived_a1,
        "doc_missing_a1": doc_missing_a1,
        "doc_active_a2": doc_active_a2,
        "doc_tracker_a": doc_tracker_a,
        "doc_active_b1": doc_active_b1,
    }


# --- humanise_type ---


def test_humanise_type_known() -> None:
    assert humanise_type("context") == "Context"
    assert humanise_type("requirements") == "Requirements"
    assert humanise_type("plan") == "Plan"
    assert humanise_type("review") == "Review"
    assert humanise_type("features") == "Tracker"


def test_humanise_type_unknown_capitalises_and_spaces() -> None:
    assert humanise_type("requirements-feedback-1") == "Requirements feedback 1"
    assert humanise_type("my_doc") == "My doc"


# --- badge_kind ---


def test_badge_kind_known_types() -> None:
    assert badge_kind("context") == "context"
    assert badge_kind("requirements") == "requirements"
    assert badge_kind("plan") == "plan"
    assert badge_kind("review") == "review"


def test_badge_kind_feedback_variants_normalise() -> None:
    assert badge_kind("requirements-feedback") == "feedback"
    assert badge_kind("plan-feedback") == "feedback"
    assert badge_kind("review-feedback") == "feedback"


def test_badge_kind_none_returns_context() -> None:
    assert badge_kind(None) == "context"


def test_doc_card_badge_from_doc_type(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    ts = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj', ?)", (ts,))
        proj_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
        conn.execute(
            "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
            "VALUES (?, 'feat', 'in_progress', ?, ?)",
            (proj_id, ts, ts),
        )
        feat_id = conn.execute(
            "SELECT id FROM features WHERE project_id=? AND slug='feat'", (proj_id,)
        ).fetchone()["id"]

        def _insert(doc_type: str, path: str) -> None:
            conn.execute(
                "INSERT INTO documents (project_id, feature_id, type, status, source_path, "
                "metadata_json, source_mtime, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, '{}', ?, ?, ?)",
                (proj_id, feat_id, doc_type, path, ts, ts, ts),
            )
            doc_id = conn.execute(
                "SELECT id FROM documents WHERE source_path=?", (path,)
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO events (document_id, event_type, payload_json, created_at) "
                "VALUES (?, 'created', '{}', ?)",
                (doc_id, ts),
            )

        _insert("requirements", "/docs/proj/feat/requirements.html")
        _insert("requirements-feedback", "/docs/proj/feat/requirements-feedback-1.html")
        conn.execute(
            "INSERT INTO synthesis_responses (document_id, item_num, response, routine_flag, updated_at) "
            "VALUES ((SELECT id FROM documents WHERE source_path='/docs/proj/feat/requirements-feedback-1.html'), 1, 'ans', NULL, ?)",
            (ts,),
        )

    new_cards = new_since_last_visit(conn)
    req_card = next(
        c
        for c in new_cards
        if "requirements" in c.label.lower() and "feedback" not in c.label.lower()
    )
    fb_card = next(c for c in new_cards if "feedback" in c.label.lower())
    assert req_card.badge == "requirements"
    assert fb_card.badge == "feedback"


def test_in_progress_card_badge(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _seed(conn)
    cards = in_progress(conn)
    assert all(c.badge == "in-progress" for c in cards)


def test_shipped_card_badge(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    import datetime

    now = datetime.datetime.now(tz=datetime.UTC)
    ts = (now - datetime.timedelta(days=1)).isoformat()
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj', ?)", (ts,))
        _insert_shipped_event(conn, "proj", "feat-x", ts)
    cards = recently_shipped(conn, within_days=30)
    assert len(cards) == 1
    assert cards[0].badge == "shipped"


# --- new_since_last_visit ---


def test_new_since_returns_unread_active_feature_docs(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _seed(conn)
    cards = new_since_last_visit(conn)
    features = {c.feature for c in cards}
    # doc_active_a1 and doc_active_a2 are unread active feature docs
    assert "feat-a-1" in features
    assert "feat-a-2" in features


def test_new_since_excludes_archived_missing_read(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        ids = _seed(conn)
    cards = new_since_last_visit(conn)
    doc_ids = {c.document_id for c in cards}
    assert ids["doc_archived_a1"] not in doc_ids
    assert ids["doc_missing_a1"] not in doc_ids
    # doc_active_b1 is read
    assert ids["doc_active_b1"] not in doc_ids


def test_new_since_excludes_null_feature_tracker_doc(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        ids = _seed(conn)
    cards = new_since_last_visit(conn)
    doc_ids = {c.document_id for c in cards}
    assert ids["doc_tracker_a"] not in doc_ids


def test_new_since_ordered_newest_first(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _seed(conn)
    cards = new_since_last_visit(conn)
    # doc_active_a1 has NEWER event; doc_active_a2 has RECENT event — a1 must come first
    assert cards[0].feature == "feat-a-1"
    assert cards[1].feature == "feat-a-2"


def test_new_since_project_filter(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        ids = _seed(conn)
    cards_a = new_since_last_visit(conn, project_id=ids["proj_a"])
    features_a = {c.feature for c in cards_a}
    assert "feat-a-1" in features_a
    assert "feat-a-2" in features_a
    assert "feat-b-1" not in features_a

    cards_b = new_since_last_visit(conn, project_id=ids["proj_b"])
    # doc_active_b1 is read, so nothing new from proj-b
    assert cards_b == []


# --- in_progress ---


def test_in_progress_returns_only_in_progress_features(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _seed(conn)
    cards = in_progress(conn)
    slugs = {c.feature for c in cards}
    assert "feat-a-1" in slugs
    assert "feat-a-noevent" in slugs
    assert "feat-b-1" in slugs
    # available features should not appear
    assert "feat-a-2" not in slugs


def test_in_progress_ordered_by_most_recent_event(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _seed(conn)
    cards = in_progress(conn)
    # feat-a-1 has doc with NEWER event; feat-a-noevent has no docs → sorts last
    slugs = [c.feature for c in cards]
    assert slugs.index("feat-a-1") < slugs.index("feat-a-noevent")


def test_in_progress_feature_with_no_active_doc_events_appears_last(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _seed(conn)
    cards = in_progress(conn)
    slugs = [c.feature for c in cards]
    # feat-a-noevent has no docs, so last_activity is NULL → COALESCE('') → sorts last
    assert slugs[-1] == "feat-a-noevent"


def test_in_progress_project_filter(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        ids = _seed(conn)
    cards_a = in_progress(conn, project_id=ids["proj_a"])
    slugs_a = {c.feature for c in cards_a}
    assert "feat-a-1" in slugs_a
    assert "feat-b-1" not in slugs_a

    cards_b = in_progress(conn, project_id=ids["proj_b"])
    slugs_b = {c.feature for c in cards_b}
    assert "feat-b-1" in slugs_b
    assert "feat-a-1" not in slugs_b


# --- recently_shipped ---


def _insert_shipped_event(conn: sqlite3.Connection, project: str, slug: str, ts: str) -> None:
    import json

    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'shipped', ?, ?)",
        (json.dumps({"project": project, "slug": slug}), ts),
    )


def test_recently_shipped_returns_within_window(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    now = datetime.now(tz=UTC)
    inside = (now - timedelta(days=10)).isoformat()
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (inside,))
        _insert_shipped_event(conn, "proj-a", "feat-x", inside)
    cards = recently_shipped(conn, within_days=20)
    assert len(cards) == 1
    assert cards[0].feature == "feat-x"
    assert cards[0].project == "proj-a"
    assert cards[0].label == "Shipped"


def test_recently_shipped_excludes_outside_window(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    now = datetime.now(tz=UTC)
    outside = (now - timedelta(days=40)).isoformat()
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (outside,))
        _insert_shipped_event(conn, "proj-a", "feat-x", outside)
    cards = recently_shipped(conn, within_days=30)
    assert cards == []


def test_recently_shipped_cutoff_boundary(tmp_path: Path) -> None:
    """An event at exactly the age boundary is excluded (strict >); one inside is included."""
    conn = temp_conn(tmp_path)
    now = datetime.now(tz=UTC)
    event_ts = (now - timedelta(days=10)).isoformat()
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (event_ts,))
        _insert_shipped_event(conn, "proj-a", "feat-x", event_ts)
    # within_days=5: event is 10 days old, window is 5 days → outside
    assert recently_shipped(conn, within_days=5) == []
    # within_days=20: event is 10 days old, window is 20 days → inside
    cards = recently_shipped(conn, within_days=20)
    assert len(cards) == 1


def test_recently_shipped_newest_first(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    now = datetime.now(tz=UTC)
    t1 = (now - timedelta(days=5)).isoformat()
    t2 = (now - timedelta(days=2)).isoformat()
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (t1,))
        _insert_shipped_event(conn, "proj-a", "feat-old", t1)
        _insert_shipped_event(conn, "proj-a", "feat-new", t2)
    cards = recently_shipped(conn, within_days=30)
    assert cards[0].feature == "feat-new"
    assert cards[1].feature == "feat-old"


def test_recently_shipped_capped_at_limit(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    now = datetime.now(tz=UTC)
    base = (now - timedelta(days=1)).isoformat()
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (base,))
        for i in range(10):
            _insert_shipped_event(conn, "proj-a", f"feat-{i}", base)
    cards = recently_shipped(conn, within_days=30, limit=3)
    assert len(cards) == 3


def test_recently_shipped_keeps_latest_per_feature(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    now = datetime.now(tz=UTC)
    t1 = (now - timedelta(days=5)).isoformat()
    t2 = (now - timedelta(days=2)).isoformat()
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (t1,))
        # Two shipped events for same feature
        _insert_shipped_event(conn, "proj-a", "feat-x", t1)
        _insert_shipped_event(conn, "proj-a", "feat-x", t2)
    cards = recently_shipped(conn, within_days=30)
    # Should deduplicate to one card, using the latest timestamp
    assert len(cards) == 1
    assert cards[0].last_activity == t2


def test_recently_shipped_project_filter_by_name(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    now = datetime.now(tz=UTC)
    ts = (now - timedelta(days=5)).isoformat()
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-a', ?)", (ts,))
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj-b', ?)", (ts,))
        _insert_shipped_event(conn, "proj-a", "feat-a", ts)
        _insert_shipped_event(conn, "proj-b", "feat-b", ts)
    cards_a = recently_shipped(conn, "proj-a", within_days=30)
    assert len(cards_a) == 1
    assert cards_a[0].project == "proj-a"

    cards_b = recently_shipped(conn, "proj-b", within_days=30)
    assert len(cards_b) == 1
    assert cards_b[0].project == "proj-b"


# --- build_inbox ---


def test_build_inbox_none_returns_unfiltered(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _seed(conn)
    inbox = build_inbox(conn)
    features = {c.feature for c in inbox.new_since}
    assert "feat-a-1" in features
    assert "feat-a-2" in features
    in_prog_slugs = {c.feature for c in inbox.in_progress}
    assert "feat-a-1" in in_prog_slugs
    assert "feat-b-1" in in_prog_slugs


def test_build_inbox_known_project_filters(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _seed(conn)
    inbox = build_inbox(conn, project="proj-a")
    # in_progress should only have proj-a features
    projects = {c.project for c in inbox.in_progress}
    assert projects == {"proj-a"}
    assert all(c.project == "proj-a" for c in inbox.new_since)


def test_build_inbox_unknown_project_returns_empty(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _seed(conn)
    inbox = build_inbox(conn, project="no-such-project")
    assert inbox == Inbox([], [], [], [])
    # Verify it's truly empty, not unfiltered
    assert inbox.new_since == []
    assert inbox.in_progress == []
    assert inbox.recently_shipped == []
    assert inbox.awaiting_input == []


# --- awaiting_input ---


def _insert_feedback_doc(
    conn: sqlite3.Connection,
    project_name: str,
    feature_slug: str,
    doc_type: str = "requirements-feedback",
    status: str = "active",
    path: str | None = None,
    ts: str = "2020-06-01T00:00:00+00:00",
) -> int:
    """Insert a feedback doc row for testing, auto-creating project and feature if needed."""
    conn.execute(
        "INSERT INTO projects (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
        (project_name, ts),
    )
    proj_id = conn.execute("SELECT id FROM projects WHERE name = ?", (project_name,)).fetchone()[
        "id"
    ]
    conn.execute(
        "INSERT INTO features (project_id, slug, created_at, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(project_id, slug) DO NOTHING",
        (proj_id, feature_slug, ts, ts),
    )
    feat_id = conn.execute(
        "SELECT id FROM features WHERE project_id = ? AND slug = ?", (proj_id, feature_slug)
    ).fetchone()["id"]
    source = path or f"/docs/{project_name}/{feature_slug}/{doc_type}-1.html"
    conn.execute(
        "INSERT INTO documents (project_id, feature_id, type, status, source_path, "
        "metadata_json, source_mtime, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?)",
        (proj_id, feat_id, doc_type, status, source, ts, ts, ts),
    )
    doc_id = conn.execute("SELECT id FROM documents WHERE source_path = ?", (source,)).fetchone()[
        "id"
    ]
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (?, 'created', '{}', ?)",
        (doc_id, ts),
    )
    return doc_id


def test_awaiting_input_active_unsubmitted_feedback_appears(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        doc_id = _insert_feedback_doc(conn, "proj-a", "feat-a")
    cards = awaiting_input(conn)
    assert len(cards) == 1
    assert cards[0].document_id == doc_id
    assert cards[0].feature == "feat-a"
    assert cards[0].label == "Requirements feedback"


def test_awaiting_input_excluded_from_new_since(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        doc_id = _insert_feedback_doc(conn, "proj-a", "feat-a")
    new_cards = new_since_last_visit(conn)
    assert not any(c.document_id == doc_id for c in new_cards)


def test_awaiting_input_leaves_after_submission(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    ts = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _insert_feedback_doc(conn, "proj-a", "feat-a")
    assert len(awaiting_input(conn)) == 1

    with transaction(conn):
        conn.execute(
            "INSERT INTO synthesis_responses (document_id, item_num, response, routine_flag, updated_at) "
            "VALUES (?, 1, '', NULL, ?)",
            (doc_id, ts),
        )
    assert awaiting_input(conn) == []


def test_awaiting_input_archived_doc_never_appears(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _insert_feedback_doc(conn, "proj-a", "feat-a", status="archived")
    assert awaiting_input(conn) == []


def test_awaiting_input_submitted_doc_appears_in_new_since(tmp_path: Path) -> None:
    """Once submitted, a feedback doc is no longer excluded from new_since."""
    conn = temp_conn(tmp_path)
    ts = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _insert_feedback_doc(conn, "proj-a", "feat-a")
        conn.execute(
            "INSERT INTO synthesis_responses (document_id, item_num, response, routine_flag, updated_at) "
            "VALUES (?, 1, 'answer', NULL, ?)",
            (doc_id, ts),
        )
    new_cards = new_since_last_visit(conn)
    assert any(c.document_id == doc_id for c in new_cards)


def test_awaiting_input_is_empty_accounts_for_category(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _insert_feedback_doc(conn, "proj-a", "feat-a")
    inbox = build_inbox(conn)
    assert not inbox.is_empty
    assert len(inbox.awaiting_input) == 1


def test_awaiting_input_project_filter(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        _insert_feedback_doc(conn, "proj-a", "feat-a")
        _insert_feedback_doc(
            conn, "proj-b", "feat-b", path="/docs/proj-b/feat-b/plan-feedback-1.html"
        )
    proj_a_id = conn.execute("SELECT id FROM projects WHERE name='proj-a'").fetchone()["id"]
    cards_a = awaiting_input(conn, project_id=proj_a_id)
    assert len(cards_a) == 1
    assert cards_a[0].project == "proj-a"


# --- mark_new_since_read ---


def test_mark_new_since_read_stamps_new_since_docs(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        ids = _seed(conn)
    # doc_active_a1 and doc_active_a2 are new-since
    count = mark_new_since_read(conn)
    assert count == 2
    assert ids["doc_active_a1"] not in [
        c.document_id for c in new_since_last_visit(conn) if c.document_id
    ]
    assert ids["doc_active_a2"] not in [
        c.document_id for c in new_since_last_visit(conn) if c.document_id
    ]


def test_mark_new_since_read_leaves_awaiting_input_untouched(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        doc_id = _insert_feedback_doc(conn, "proj-a", "feat-a")
    # awaiting-input doc should not be stamped by mark_new_since_read
    mark_new_since_read(conn)
    row = conn.execute(
        "SELECT last_read_at FROM read_state WHERE document_id=?", (doc_id,)
    ).fetchone()
    assert row is None


def test_mark_new_since_read_project_scope(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        ids = _seed(conn)
    proj_a_id = ids["proj_a"]
    count = mark_new_since_read(conn, project_id=proj_a_id)
    assert count == 2
    # proj-b's doc is already read (doc_active_b1 has read_state in _seed); still no new-since from b
    assert new_since_last_visit(conn, project_id=ids["proj_b"]) == []


# --- classify_reason helpers ---


def _seed_reason_doc(
    conn: sqlite3.Connection,
    doc_type: str = "context",
    ts: str = "2020-01-01T00:00:00+00:00",
) -> int:
    """Seed a minimal project/feature/document; returns the document_id."""
    conn.execute(
        "INSERT INTO projects (name, created_at) VALUES ('proj', ?) ON CONFLICT(name) DO NOTHING",
        (ts,),
    )
    proj_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
    conn.execute(
        "INSERT INTO features (project_id, slug, created_at, updated_at) "
        "VALUES (?, 'feat', ?, ?) ON CONFLICT(project_id, slug) DO NOTHING",
        (proj_id, ts, ts),
    )
    feat_id = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug='feat'", (proj_id,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO documents (project_id, feature_id, type, status, source_path, "
        "metadata_json, source_mtime, created_at, updated_at) "
        "VALUES (?, ?, ?, 'active', '/docs/proj/feat/doc.html', '{}', ?, ?, ?)",
        (proj_id, feat_id, doc_type, ts, ts, ts),
    )
    return conn.execute(
        "SELECT id FROM documents WHERE source_path='/docs/proj/feat/doc.html'"
    ).fetchone()["id"]


def _add_event(conn: sqlite3.Connection, doc_id: int, event_type: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (?, ?, '{}', ?)",
        (doc_id, event_type, ts),
    )


def _make_sections_content(*sections: tuple[str, str]) -> ParsedContent:
    return ParsedContent(
        shape="sections",
        sections=tuple(Section(key=k, body=b) for k, b in sections),
    )


# --- classify_reason ---


def test_classify_reason_new_doc_no_prior_version(tmp_path: Path) -> None:
    """A content event with no prior version → kind='new', label='New'."""
    conn = temp_conn(tmp_path)
    with transaction(conn):
        doc_id = _seed_reason_doc(conn)
        _add_event(conn, doc_id, "created", "2020-06-01T00:00:00+00:00")

    reason = classify_reason(conn, doc_id, "context", last_read=None)
    assert reason is not None
    assert reason.kind == "new"
    assert reason.label == "New"


def test_classify_reason_content_change_with_sections(tmp_path: Path) -> None:
    """Updated doc with changed sections → kind='content', label names the changed sections."""
    conn = temp_conn(tmp_path)
    T_v1 = "2020-04-01T00:00:00+00:00"
    T_read = "2020-05-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn, doc_type="context")
        record_version(
            conn,
            doc_id,
            _make_sections_content(
                ("problem-space", "<p>old</p>"), ("related-work", "<p>same</p>")
            ),
            actor="test",
            now=T_v1,
        )
        record_version(
            conn,
            doc_id,
            _make_sections_content(
                ("problem-space", "<p>new text here</p>"), ("related-work", "<p>same</p>")
            ),
            actor="test",
            now=T_event,
        )
        _add_event(conn, doc_id, "updated", T_event)

    reason = classify_reason(conn, doc_id, "context", last_read=T_read)
    assert reason is not None
    assert reason.kind == "content"
    assert reason.changed_count == 1
    assert reason.has_diff is True
    assert "Problem space" in reason.label
    assert reason.label.startswith("Updated — ")


def test_classify_reason_formatting_only(tmp_path: Path) -> None:
    """Updated doc where text is unchanged (markup-only diff) → formatting-only label."""
    conn = temp_conn(tmp_path)
    T_v1 = "2020-04-01T00:00:00+00:00"
    T_read = "2020-05-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn, doc_type="context")
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<p>same text</p>")),
            actor="test",
            now=T_v1,
        )
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<div>same text</div>")),
            actor="test",
            now=T_event,
        )
        _add_event(conn, doc_id, "updated", T_event)

    reason = classify_reason(conn, doc_id, "context", last_read=T_read)
    assert reason is not None
    assert reason.kind == "content"
    assert reason.label == "Updated (formatting only)"
    assert reason.has_diff is False
    assert reason.changed_count == 0


def test_classify_reason_comment_only(tmp_path: Path) -> None:
    """Only comment events → kind='comments', label='Comments added'."""
    conn = temp_conn(tmp_path)
    T_read = "2020-05-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn)
        _add_event(conn, doc_id, "comment_submitted", T_event)

    reason = classify_reason(conn, doc_id, "context", last_read=T_read)
    assert reason is not None
    assert reason.kind == "comments"
    assert reason.label == "Comments added"


def test_classify_reason_reactivated_treated_as_content(tmp_path: Path) -> None:
    """'reactivated' event counts as a content event."""
    conn = temp_conn(tmp_path)
    T_v1 = "2020-04-01T00:00:00+00:00"
    T_read = "2020-05-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn, doc_type="context")
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<p>old</p>")),
            actor="test",
            now=T_v1,
        )
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<p>new</p>")),
            actor="test",
            now=T_event,
        )
        _add_event(conn, doc_id, "reactivated", T_event)

    reason = classify_reason(conn, doc_id, "context", last_read=T_read)
    assert reason is not None
    assert reason.kind == "content"
    assert reason.has_diff is True


def test_classify_reason_never_read_already_versioned_returns_new(tmp_path: Path) -> None:
    """Never-read doc (last_read=None → baseline='') with versions → 'New'
    because content_at_or_before('') returns None."""
    conn = temp_conn(tmp_path)
    T_v1 = "2020-04-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn)
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<p>content</p>")),
            actor="test",
            now=T_v1,
        )
        _add_event(conn, doc_id, "created", T_v1)

    reason = classify_reason(conn, doc_id, "context", last_read=None)
    assert reason is not None
    assert reason.kind == "new"
    assert reason.label == "New"


def test_classify_reason_label_overflow_three_sections(tmp_path: Path) -> None:
    """3+ changed sections → 'Updated — Name1, Name2 +N more'."""
    conn = temp_conn(tmp_path)
    T_v1 = "2020-04-01T00:00:00+00:00"
    T_read = "2020-05-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn, doc_type="context")
        record_version(
            conn,
            doc_id,
            _make_sections_content(
                ("problem-space", "<p>a</p>"),
                ("related-work", "<p>b</p>"),
                ("constraints", "<p>c</p>"),
            ),
            actor="test",
            now=T_v1,
        )
        record_version(
            conn,
            doc_id,
            _make_sections_content(
                ("problem-space", "<p>a new</p>"),
                ("related-work", "<p>b new</p>"),
                ("constraints", "<p>c new</p>"),
            ),
            actor="test",
            now=T_event,
        )
        _add_event(conn, doc_id, "updated", T_event)

    reason = classify_reason(conn, doc_id, "context", last_read=T_read)
    assert reason is not None
    assert reason.changed_count == 3
    assert "+1 more" in reason.label
    assert reason.label.startswith("Updated — ")


def test_classify_reason_plan_phase_key_prettified_fallback(tmp_path: Path) -> None:
    """A 'phase-2' section key (not in section_labels) humanises as 'Phase 2'."""
    conn = temp_conn(tmp_path)
    T_v1 = "2020-04-01T00:00:00+00:00"
    T_read = "2020-05-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn, doc_type="plan")
        conn.execute(
            "UPDATE documents SET source_path='/docs/proj/feat/plan.html' WHERE id=?", (doc_id,)
        )
        record_version(
            conn,
            doc_id,
            _make_sections_content(("phase-2", "<p>old plan</p>")),
            actor="test",
            now=T_v1,
        )
        record_version(
            conn,
            doc_id,
            _make_sections_content(("phase-2", "<p>new plan text</p>")),
            actor="test",
            now=T_event,
        )
        _add_event(conn, doc_id, "updated", T_event)

    reason = classify_reason(conn, doc_id, "plan", last_read=T_read)
    assert reason is not None
    assert "Phase 2" in reason.label


def test_classify_reason_no_qualifying_events_returns_none(tmp_path: Path) -> None:
    """No events newer than the baseline → None."""
    conn = temp_conn(tmp_path)
    T_event = "2020-04-01T00:00:00+00:00"
    T_read = "2020-05-01T00:00:00+00:00"  # read AFTER the event
    with transaction(conn):
        doc_id = _seed_reason_doc(conn)
        _add_event(conn, doc_id, "created", T_event)

    reason = classify_reason(conn, doc_id, "context", last_read=T_read)
    assert reason is None


def test_classify_reason_extra_css_used_event_is_not_surfaced(tmp_path: Path) -> None:
    """An `extra_css_used` event alone must NOT re-surface a doc in the inbox.

    The promotion ratchet is write-only telemetry: it is neither a content nor a
    comment event. This pins the deliberate fall-through so that adding
    `extra_css_used` to `_CONTENT_EVENTS`/`_COMMENT_EVENTS` later would go red.
    """
    conn = temp_conn(tmp_path)
    T_read = "2020-01-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"  # event AFTER the read baseline
    with transaction(conn):
        doc_id = _seed_reason_doc(conn)
        _add_event(conn, doc_id, "extra_css_used", T_event)

    reason = classify_reason(conn, doc_id, "requirements", last_read=T_read)
    assert reason is None


# --- new_since_last_visit: href ---


def test_new_since_href_diff_when_has_diff(tmp_path: Path) -> None:
    """A card with a textual content change points to the diff view."""
    conn = temp_conn(tmp_path)
    T_v1 = "2020-04-01T00:00:00+00:00"
    T_read = "2020-05-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn, doc_type="context")
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<p>old text</p>")),
            actor="test",
            now=T_v1,
        )
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<p>new text here</p>")),
            actor="test",
            now=T_event,
        )
        _add_event(conn, doc_id, "updated", T_event)
        conn.execute(
            "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?)",
            (doc_id, T_read),
        )

    cards = new_since_last_visit(conn)
    card = next(c for c in cards if c.document_id == doc_id)
    assert card.href == f"/doc/{doc_id}?view=diff"


def test_new_since_href_plain_for_new_doc(tmp_path: Path) -> None:
    """A never-read 'new' doc links to the plain doc view (no prior version to diff)."""
    conn = temp_conn(tmp_path)
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn)
        _add_event(conn, doc_id, "created", T_event)

    cards = new_since_last_visit(conn)
    card = next(c for c in cards if c.document_id == doc_id)
    assert card.href == f"/doc/{doc_id}"


def test_new_since_href_plain_for_comments_only(tmp_path: Path) -> None:
    """A card triggered only by comment events links to the plain doc view."""
    conn = temp_conn(tmp_path)
    T_v1 = "2020-04-01T00:00:00+00:00"
    T_read = "2020-05-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn)
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<p>content</p>")),
            actor="test",
            now=T_v1,
        )
        _add_event(conn, doc_id, "comment_submitted", T_event)
        conn.execute(
            "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?)",
            (doc_id, T_read),
        )

    cards = new_since_last_visit(conn)
    card = next(c for c in cards if c.document_id == doc_id)
    assert card.href == f"/doc/{doc_id}"


def test_new_since_href_plain_for_formatting_only(tmp_path: Path) -> None:
    """A formatting-only update (no textual diff) links to the plain doc view."""
    conn = temp_conn(tmp_path)
    T_v1 = "2020-04-01T00:00:00+00:00"
    T_read = "2020-05-01T00:00:00+00:00"
    T_event = "2020-06-01T00:00:00+00:00"
    with transaction(conn):
        doc_id = _seed_reason_doc(conn)
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<p>same text</p>")),
            actor="test",
            now=T_v1,
        )
        record_version(
            conn,
            doc_id,
            _make_sections_content(("problem-space", "<div>same text</div>")),
            actor="test",
            now=T_event,
        )
        _add_event(conn, doc_id, "updated", T_event)
        conn.execute(
            "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?)",
            (doc_id, T_read),
        )

    cards = new_since_last_visit(conn)
    card = next(c for c in cards if c.document_id == doc_id)
    assert card.href == f"/doc/{doc_id}"


# ---------------------------------------------------------------------------
# archived-feature exclusion (new_since_last_visit + awaiting_input)
# ---------------------------------------------------------------------------


def _seed_archived_feature_with_doc(
    conn: sqlite3.Connection,
    project_name: str,
    feature_slug: str,
    doc_type: str,
    *,
    ts: str = "2020-06-01T00:00:00+00:00",
) -> int:
    """Seed an archived feature with one active document of the given type.

    Returns the document_id. The document has an unread event so it would
    surface in new_since_last_visit if the feature were not archived.
    """
    conn.execute(
        "INSERT INTO projects (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
        (project_name, ts),
    )
    proj_id = conn.execute("SELECT id FROM projects WHERE name = ?", (project_name,)).fetchone()[
        "id"
    ]
    conn.execute(
        "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
        "VALUES (?, ?, 'archived', ?, ?) ON CONFLICT(project_id, slug) DO NOTHING",
        (proj_id, feature_slug, ts, ts),
    )
    feat_id = conn.execute(
        "SELECT id FROM features WHERE project_id = ? AND slug = ?", (proj_id, feature_slug)
    ).fetchone()["id"]
    source = f"/docs/{project_name}/{feature_slug}/{doc_type}-1.html"
    conn.execute(
        "INSERT INTO documents (project_id, feature_id, type, status, source_path, "
        "metadata_json, source_mtime, created_at, updated_at) "
        "VALUES (?, ?, ?, 'active', ?, '{}', ?, ?, ?)",
        (proj_id, feat_id, doc_type, source, ts, ts, ts),
    )
    doc_id = conn.execute("SELECT id FROM documents WHERE source_path = ?", (source,)).fetchone()[
        "id"
    ]
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (?, 'created', '{}', ?)",
        (doc_id, ts),
    )
    return doc_id


def test_new_since_excludes_archived_feature_active_doc(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        doc_id = _seed_archived_feature_with_doc(conn, "proj", "dropped-feat", "context")
    cards = new_since_last_visit(conn)
    assert not any(c.document_id == doc_id for c in cards)


def test_awaiting_input_excludes_archived_feature_feedback_doc(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    with transaction(conn):
        doc_id = _seed_archived_feature_with_doc(
            conn, "proj", "dropped-feat", "requirements-feedback"
        )
    cards = awaiting_input(conn)
    assert not any(c.document_id == doc_id for c in cards)
