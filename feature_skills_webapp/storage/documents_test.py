"""Unit tests for storage/documents.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from feature_skills_webapp.storage.db import connect, migrate, transaction
from feature_skills_webapp.storage.documents import (
    MAX_BODY_BYTES,
    SubmitError,
    SubmitResult,
    build_content,
    submit_document,
    validate_writable,
)
from feature_skills_webapp.storage.tracker import FeatureNotFound
from feature_skills_webapp.storage.versions import (
    backfill_logical_keys,
    current_content,
)
from feature_skills_webapp.storage.walker import logical_key, upsert_feature, upsert_project, walk

_REQUIREMENTS_BODY = "<h2>Summary</h2><p>The summary.</p>"

_REQUIREMENTS_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="requirements">
<title>Test Requirements</title>
</head>
<body>
<main class="document">
<section id="summary"><p>The summary.</p></section>
<section id="scope"><p>The scope.</p></section>
</main>
</body>
</html>
"""


def temp_conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    backfill_logical_keys(conn)
    return conn


def make_submit(
    conn: sqlite3.Connection,
    *,
    project: str = "proj",
    feature: str = "feat-a",
    doc_type: str = "requirements",
    instance: int = 1,
    sections: dict[str, str] | None = None,
    now: str = "2024-01-01T00:00:00+00:00",
) -> SubmitResult:
    if sections is None:
        sections = {"summary": _REQUIREMENTS_BODY}
    content = build_content(doc_type, sections, None)
    with transaction(conn):
        project_id = upsert_project(conn, project, now)
        upsert_feature(conn, project_id, feature, now)
        return submit_document(
            conn,
            project=project,
            feature=feature,
            doc_type=doc_type,
            instance=instance,
            content=content,
            actor="test-agent",
            now=now,
        )


# --- validate_writable ---


def test_validate_writable_accepts_context():
    validate_writable("context", "feat", 1)


def test_validate_writable_accepts_requirements():
    validate_writable("requirements", "feat", 1)


def test_validate_writable_accepts_plan():
    validate_writable("plan", "feat", 1)


def test_validate_writable_accepts_feedback():
    validate_writable("requirements-feedback", "feat", 2)


def test_validate_writable_rejects_features():
    with pytest.raises(SubmitError, match="not writable"):
        validate_writable("features", "feat", 1)


def test_validate_writable_rejects_unknown_type():
    with pytest.raises(SubmitError, match="not writable"):
        validate_writable("review", "feat", 1)


def test_validate_writable_rejects_feature_none():
    with pytest.raises(SubmitError, match="feature must be specified"):
        validate_writable("requirements", None, 1)


def test_validate_writable_rejects_instance_not_1_for_section_doc():
    with pytest.raises(SubmitError, match="instance must be 1"):
        validate_writable("requirements", "feat", 2)


def test_validate_writable_allows_instance_gt_1_for_feedback():
    validate_writable("requirements-feedback", "feat", 3)  # no exception


# --- build_content ---


def test_build_content_section_doc_manifest_order():
    # Supply keys out of manifest order; expect manifest order in result
    sections = {"scope": "<p>Scope.</p>", "summary": "<p>Summary.</p>"}
    result = build_content("requirements", sections, None)
    assert result.shape == "sections"
    assert len(result.sections) == 2
    assert result.sections[0].key == "summary"  # manifest order: summary before scope
    assert result.sections[1].key == "scope"


def test_build_content_section_doc_missing_keys_tolerated():
    sections = {"summary": "<p>Summary.</p>"}
    result = build_content("requirements", sections, None)
    assert len(result.sections) == 1
    assert result.sections[0].key == "summary"


def test_build_content_section_doc_unknown_key_raises():
    with pytest.raises(SubmitError, match="unknown section key"):
        build_content("requirements", {"nonexistent": "<p>x</p>"}, None)


def test_build_content_opaque_stores_single_body():
    result = build_content("requirements-feedback", None, "<p>Feedback body.</p>")
    assert result.shape == "opaque"
    assert len(result.sections) == 1
    assert result.sections[0].key == ""
    assert result.sections[0].body == "<p>Feedback body.</p>"


def test_build_content_opaque_rejects_sections():
    with pytest.raises(SubmitError, match="'sections' is not accepted"):
        build_content("requirements-feedback", {"x": "y"}, None)


def test_build_content_section_doc_rejects_body():
    with pytest.raises(SubmitError, match="'body' is not accepted"):
        build_content("requirements", None, "<p>body</p>")


def test_build_content_opaque_missing_body_raises():
    with pytest.raises(SubmitError, match="'body' is required"):
        build_content("requirements-feedback", None, None)


def test_build_content_section_doc_missing_sections_raises():
    with pytest.raises(SubmitError, match="'sections' is required"):
        build_content("requirements", None, None)


def test_build_content_oversize_body_raises():
    big = "x" * (MAX_BODY_BYTES + 1)
    with pytest.raises(SubmitError, match="exceeds 1 MB"):
        build_content("requirements-feedback", None, big)


def test_build_content_oversize_section_raises():
    big = "x" * (MAX_BODY_BYTES + 1)
    with pytest.raises(SubmitError, match="exceeds 1 MB"):
        build_content("requirements", {"summary": big}, None)


def test_build_content_plan_repeated_prefix_accepted():
    sections = {
        "overview": "<p>Overview.</p>",
        "phase-1": "<p>Phase 1.</p>",
        "phase-2": "<p>Phase 2.</p>",
    }
    result = build_content("plan", sections, None)
    keys = [s.key for s in result.sections]
    assert "overview" in keys
    assert "phase-1" in keys
    assert "phase-2" in keys
    # repeated-prefix keys come after fixed keys
    assert keys.index("overview") < keys.index("phase-1")


def test_build_content_plan_repeated_prefix_sorted():
    # Provide out of order — expect sorted
    sections = {"phase-2": "<p>P2</p>", "phase-1": "<p>P1</p>"}
    result = build_content("plan", sections, None)
    repeated = [s.key for s in result.sections if s.key.startswith("phase-")]
    assert repeated == ["phase-1", "phase-2"]


# --- submit_document ---


def test_submit_creates_row_with_correct_fields(tmp_path: Path):
    conn = temp_conn(tmp_path)
    result = make_submit(conn)

    assert result.created is True
    assert result.changed is True
    assert result.version_num == 1
    assert result.logical_key == "proj/feat-a/requirements/1"

    row = conn.execute(
        "SELECT type, status, source_path, metadata_json FROM documents WHERE id=?",
        (result.document_id,),
    ).fetchone()
    assert row["type"] == "requirements"
    assert row["status"] == "active"
    assert row["source_path"] is None

    import json

    meta = json.loads(row["metadata_json"])
    assert meta["title"] == "feat-a — Requirements"


def test_submit_emits_created_event(tmp_path: Path):
    conn = temp_conn(tmp_path)
    result = make_submit(conn)

    events = conn.execute(
        "SELECT event_type FROM events WHERE document_id=?", (result.document_id,)
    ).fetchall()
    assert [e["event_type"] for e in events] == ["created"]


def test_submit_update_cuts_new_version_and_updated_event(tmp_path: Path):
    conn = temp_conn(tmp_path)
    r1 = make_submit(conn, now="2024-01-01T00:00:00+00:00")
    r2 = make_submit(conn, sections={"summary": "<p>Changed.</p>"}, now="2024-01-02T00:00:00+00:00")

    assert r1.document_id == r2.document_id
    assert r2.created is False
    assert r2.changed is True
    assert r2.version_num == 2

    events = conn.execute(
        "SELECT event_type FROM events WHERE document_id=? ORDER BY rowid",
        (r1.document_id,),
    ).fetchall()
    assert [e["event_type"] for e in events] == ["created", "updated"]


def test_submit_identical_content_cuts_no_version(tmp_path: Path):
    conn = temp_conn(tmp_path)
    r1 = make_submit(conn, now="2024-01-01T00:00:00+00:00")
    r2 = make_submit(conn, now="2024-01-02T00:00:00+00:00")  # same sections

    assert r2.changed is False
    assert r2.version_num == 1  # still v1

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM document_versions WHERE document_id=?", (r1.document_id,)
    ).fetchone()["n"]
    assert count == 1


def test_submit_identical_content_emits_no_extra_event(tmp_path: Path):
    conn = temp_conn(tmp_path)
    r1 = make_submit(conn, now="2024-01-01T00:00:00+00:00")
    make_submit(conn, now="2024-01-02T00:00:00+00:00")  # identical

    events = conn.execute(
        "SELECT event_type FROM events WHERE document_id=?", (r1.document_id,)
    ).fetchall()
    assert len(events) == 1  # only the 'created' event


def test_submit_cur_is_none_seeds_version_silently(tmp_path: Path):
    """Row exists with no version — seed v1 with no event."""
    conn = temp_conn(tmp_path)
    now = "2024-01-01T00:00:00+00:00"

    # Insert a row manually with no version
    with transaction(conn):
        conn.execute(
            "INSERT INTO projects (name, created_at) VALUES ('proj', ?) ON CONFLICT DO NOTHING",
            (now,),
        )
        proj_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
        conn.execute(
            "INSERT INTO features (project_id, slug, created_at, updated_at) VALUES (?, 'feat-a', ?, ?) "
            "ON CONFLICT DO NOTHING",
            (proj_id, now, now),
        )
        feat_id = conn.execute(
            "SELECT id FROM features WHERE project_id=? AND slug='feat-a'", (proj_id,)
        ).fetchone()["id"]
        lkey = logical_key("proj", "feat-a", "requirements", 1)
        conn.execute(
            "INSERT INTO documents "
            "(project_id, feature_id, type, status, source_path, logical_key, instance, "
            "metadata_json, source_mtime, created_at, updated_at) "
            "VALUES (?, ?, 'requirements', 'active', NULL, ?, 1, '{}', NULL, ?, ?)",
            (proj_id, feat_id, lkey, now, now),
        )
        doc_id = conn.execute("SELECT id FROM documents WHERE logical_key=?", (lkey,)).fetchone()[
            "id"
        ]

    # No versions exist yet
    assert current_content(conn, doc_id) is None

    result = make_submit(conn, now="2024-01-02T00:00:00+00:00")

    assert result.document_id == doc_id
    assert result.created is False
    assert result.changed is True
    assert result.version_num == 1

    # No event emitted (seeding is silent)
    events = conn.execute("SELECT event_type FROM events WHERE document_id=?", (doc_id,)).fetchall()
    assert events == []


# --- Convergence keystone ---


def test_convergence_file_import_then_api_submit_same_row(tmp_path: Path):
    """Walk-import a file doc, then API-submit the same identity → one row, same document_id."""
    docs_root = tmp_path / "docs"
    feat_dir = docs_root / "proj1" / "feat-x"
    feat_dir.mkdir(parents=True)
    (feat_dir / "requirements.html").write_text(_REQUIREMENTS_HTML)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    file_row = conn.execute(
        "SELECT id FROM documents WHERE logical_key=?",
        ("proj1/feat-x/requirements/1",),
    ).fetchone()
    assert file_row is not None
    file_doc_id = file_row["id"]

    # API submit the same identity with different content
    content = build_content("requirements", {"summary": "<p>API content.</p>"}, None)
    with transaction(conn):
        api_result = submit_document(
            conn,
            project="proj1",
            feature="feat-x",
            doc_type="requirements",
            instance=1,
            content=content,
            actor="agent",
            now="2024-06-01T00:00:00+00:00",
        )

    # Same row
    assert api_result.document_id == file_doc_id
    assert api_result.created is False


def test_convergence_api_submit_different_content_increments_version(tmp_path: Path):
    """Submitting different content after a file-import cuts a new version."""
    docs_root = tmp_path / "docs"
    feat_dir = docs_root / "proj1" / "feat-x"
    feat_dir.mkdir(parents=True)
    (feat_dir / "requirements.html").write_text(_REQUIREMENTS_HTML)

    conn = temp_conn(tmp_path)
    walk(conn, docs_root, reconcile=False)

    content = build_content("requirements", {"summary": "<p>Different.</p>"}, None)
    with transaction(conn):
        result = submit_document(
            conn,
            project="proj1",
            feature="feat-x",
            doc_type="requirements",
            instance=1,
            content=content,
            actor="agent",
            now="2024-06-01T00:00:00+00:00",
        )

    assert result.changed is True
    assert result.version_num == 2


# --- Reconcile-safety keystone ---


def test_reconcile_safety_api_doc_not_marked_missing(tmp_path: Path):
    """An API-native (path-less) doc is never marked 'missing' by a reconcile walk that doesn't see it."""
    docs_root = tmp_path / "docs"
    # Create a completely separate file doc so the walk has something to reconcile against
    other_dir = docs_root / "proj1" / "other-feat"
    other_dir.mkdir(parents=True)
    (other_dir / "context.html").write_text(
        "<!DOCTYPE html><html><head>"
        '<meta name="feature-doc-type" content="context"><title>x</title></head>'
        '<body><main class="document"><section id="problem-space"><p>x</p></section></main>'
        "</body></html>"
    )

    conn = temp_conn(tmp_path)

    # Create an API doc (no file)
    content = build_content("requirements", {"summary": "<p>API only.</p>"}, None)
    with transaction(conn):
        pid = upsert_project(conn, "proj1", "2024-01-01T00:00:00+00:00")
        upsert_feature(conn, pid, "api-feat", "2024-01-01T00:00:00+00:00")
        api_result = submit_document(
            conn,
            project="proj1",
            feature="api-feat",
            doc_type="requirements",
            instance=1,
            content=content,
            actor="agent",
            now="2024-01-01T00:00:00+00:00",
        )

    # Reconcile walk over a root that doesn't contain the API doc
    walk(conn, docs_root, reconcile=True)

    row = conn.execute(
        "SELECT status FROM documents WHERE id=?", (api_result.document_id,)
    ).fetchone()
    assert row["status"] == "active"

    missing_events = conn.execute(
        "SELECT id FROM events WHERE document_id=? AND event_type='missing'",
        (api_result.document_id,),
    ).fetchall()
    assert missing_events == []


# --- submit_document — FeatureNotFound ---


def test_submit_raises_feature_not_found_when_feature_absent(tmp_path: Path):
    conn = temp_conn(tmp_path)
    content = build_content("requirements", {"summary": "<p>x</p>"}, None)
    now = "2024-01-01T00:00:00+00:00"
    upsert_project(conn, "proj", now)
    with pytest.raises(FeatureNotFound), transaction(conn):
        submit_document(
            conn,
            project="proj",
            feature="nonexistent",
            doc_type="requirements",
            instance=1,
            content=content,
            actor="agent",
            now=now,
        )


def test_submit_raises_project_not_found_when_project_absent(tmp_path: Path):
    conn = temp_conn(tmp_path)
    content = build_content("requirements", {"summary": "<p>x</p>"}, None)
    from feature_skills_webapp.storage.tracker import ProjectNotFound

    with pytest.raises(ProjectNotFound), transaction(conn):
        submit_document(
            conn,
            project="nonexistent",
            feature=None,
            doc_type="requirements",
            instance=1,
            content=content,
            actor="agent",
            now="2024-01-01T00:00:00+00:00",
        )


# ---------------------------------------------------------------------------
# extra_css — build_content validation + event exactness
# ---------------------------------------------------------------------------


def test_build_content_extra_css_stored() -> None:
    c = build_content("requirements", {"summary": "<p>x</p>"}, None, "table{color:red}")
    assert c.extra_css == "table{color:red}"


def test_build_content_extra_css_whitespace_normalises_to_empty() -> None:
    c = build_content("requirements", {"summary": "<p>x</p>"}, None, "   \n  ")
    assert c.extra_css == ""


def test_build_content_extra_css_absent_is_empty() -> None:
    c = build_content("requirements", {"summary": "<p>x</p>"}, None, None)
    assert c.extra_css == ""


def test_build_content_extra_css_rejected_for_opaque() -> None:
    with pytest.raises(SubmitError, match="opaque"):
        build_content("requirements-feedback", None, "<p>body</p>", "p{color:red}")


def test_build_content_extra_css_with_stray_brace_rejected() -> None:
    # A leading/stray } would close the @scope block early and let the rule
    # bleed into the page chrome — reject it at the write boundary.
    with pytest.raises(SubmitError, match="unmatched '}'"):
        build_content("requirements", {"summary": "<p>x</p>"}, None, "} .crumbs { display:none }")


def test_build_content_extra_css_with_brace_inside_string_accepted() -> None:
    # A } inside a CSS string is not a structural brace — must not be rejected.
    c = build_content("requirements", {"summary": "<p>x</p>"}, None, 'td::before{content:"}"}')
    assert c.extra_css == 'td::before{content:"}"}'


def test_build_content_extra_css_with_style_close_tag_rejected() -> None:
    # A literal </style> would terminate the chrome's <style> element and inject
    # the rest as live markup — reject it at the write boundary.
    with pytest.raises(SubmitError, match="</style>"):
        build_content(
            "requirements",
            {"summary": "<p>x</p>"},
            None,
            "table{color:red} </style><script>alert(1)</script>",
        )


def test_build_content_extra_css_with_comment_open_rejected() -> None:
    with pytest.raises(SubmitError, match="</style>"):
        build_content("requirements", {"summary": "<p>x</p>"}, None, "table{color:red} <!-- x")


def _submit(
    conn: sqlite3.Connection, extra_css: str = "", *, now: str = "2024-01-01T00:00:00+00:00"
) -> SubmitResult:
    content = build_content("requirements", {"summary": "<p>x</p>"}, None, extra_css or None)
    with transaction(conn):
        project_id = upsert_project(conn, "proj", now)
        upsert_feature(conn, project_id, "feat", now)
        return submit_document(
            conn,
            project="proj",
            feature="feat",
            doc_type="requirements",
            instance=1,
            content=content,
            actor="agent",
            now=now,
        )


def _events(conn: sqlite3.Connection, doc_id: int, event_type: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id FROM events WHERE document_id=? AND event_type=?",
        (doc_id, event_type),
    ).fetchall()


def test_extra_css_used_event_on_insert(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    result = _submit(conn, "table{border:1px solid red}")
    assert len(_events(conn, result.document_id, "extra_css_used")) == 1


def test_extra_css_used_event_on_change(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    _submit(conn, now="2024-01-01T00:00:00+00:00")
    result = _submit(conn, "p{color:blue}", now="2024-01-02T00:00:00+00:00")
    assert len(_events(conn, result.document_id, "extra_css_used")) == 1


def test_no_extra_css_used_event_when_empty(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    result = _submit(conn, "")
    assert _events(conn, result.document_id, "extra_css_used") == []


def test_no_extra_css_used_event_on_identical_reput(tmp_path: Path) -> None:
    conn = temp_conn(tmp_path)
    result = _submit(conn, "table{color:red}", now="2024-01-01T00:00:00+00:00")
    result2 = _submit(conn, "table{color:red}", now="2024-01-02T00:00:00+00:00")
    assert result2.changed is False
    assert len(_events(conn, result.document_id, "extra_css_used")) == 1  # only the insert
