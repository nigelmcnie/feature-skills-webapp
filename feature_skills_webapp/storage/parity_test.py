"""Tests for storage.parity — normalised DB-to-DB section comparison."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from feature_skills_webapp.storage.db import connect, migrate, now_iso, transaction
from feature_skills_webapp.storage.documents import build_content, submit_document
from feature_skills_webapp.storage.parity import compare_dbs
from feature_skills_webapp.storage.versions import backfill_logical_keys
from feature_skills_webapp.storage.walker import upsert_feature, upsert_project


def _fresh_conn(tmp_path: Path, name: str) -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    migrate(conn)
    backfill_logical_keys(conn)
    return conn


def _write_context(
    conn: sqlite3.Connection,
    project: str,
    feature: str,
    problem_text: str,
    *,
    actor: str = "agent",
) -> None:
    content = build_content("context", {"problem-space": f"<p>{problem_text}</p>"}, None)
    now = now_iso()
    with transaction(conn):
        project_id = upsert_project(conn, project, now)
        upsert_feature(conn, project_id, feature, now)
        submit_document(
            conn,
            project=project,
            feature=feature,
            doc_type="context",
            instance=1,
            content=content,
            actor=actor,
            now=now,
        )


def test_identical_content_reports_no_mismatch(tmp_path: Path) -> None:
    conn_a = _fresh_conn(tmp_path, "a.db")
    conn_b = _fresh_conn(tmp_path, "b.db")
    _write_context(conn_a, "proj", "feat", "Hello world")
    _write_context(conn_b, "proj", "feat", "Hello world")

    report = compare_dbs(conn_a, conn_b)

    assert report.ok
    assert report.mismatches == ()
    assert report.only_in_a == ()
    assert report.only_in_b == ()


def test_differing_section_is_reported(tmp_path: Path) -> None:
    conn_a = _fresh_conn(tmp_path, "a.db")
    conn_b = _fresh_conn(tmp_path, "b.db")
    _write_context(conn_a, "proj", "feat", "Hello world")
    _write_context(conn_b, "proj", "feat", "Goodbye world")

    report = compare_dbs(conn_a, conn_b)

    assert not report.ok
    assert len(report.mismatches) == 1
    mm = report.mismatches[0]
    assert mm.logical_key == "proj/feat/context/1"
    assert mm.section_key == "problem-space"
    assert "Hello" in mm.text_a
    assert "Goodbye" in mm.text_b


def test_whitespace_and_comment_differences_are_normalised(tmp_path: Path) -> None:
    """HTML bodies differing only in whitespace or HTML comments compare equal."""
    conn_a = _fresh_conn(tmp_path, "a.db")
    conn_b = _fresh_conn(tmp_path, "b.db")
    # DB A: extra internal whitespace
    content_a = build_content("context", {"problem-space": "<p>  Hello   world  </p>"}, None)
    # DB B: HTML comment added; no extra whitespace — both extract to "Hello world"
    content_b = build_content("context", {"problem-space": "<!-- note --><p>Hello world</p>"}, None)
    now_a = now_iso()
    with transaction(conn_a):
        pid_a = upsert_project(conn_a, "proj", now_a)
        upsert_feature(conn_a, pid_a, "feat", now_a)
        submit_document(
            conn_a,
            project="proj",
            feature="feat",
            doc_type="context",
            instance=1,
            content=content_a,
            actor="importer",
            now=now_a,
        )
    now_b = now_iso()
    with transaction(conn_b):
        pid_b = upsert_project(conn_b, "proj", now_b)
        upsert_feature(conn_b, pid_b, "feat", now_b)
        submit_document(
            conn_b,
            project="proj",
            feature="feat",
            doc_type="context",
            instance=1,
            content=content_b,
            actor="agent",
            now=now_b,
        )

    report = compare_dbs(conn_a, conn_b)

    assert report.ok
    assert report.mismatches == ()


def test_actor_difference_is_ignored(tmp_path: Path) -> None:
    """actor column lives in document_versions, not in content — invisible to compare_dbs."""
    conn_a = _fresh_conn(tmp_path, "a.db")
    conn_b = _fresh_conn(tmp_path, "b.db")
    _write_context(conn_a, "proj", "feat", "Hello world", actor="importer")
    _write_context(conn_b, "proj", "feat", "Hello world", actor="agent")

    report = compare_dbs(conn_a, conn_b)

    assert report.ok
    assert report.mismatches == ()


def test_source_path_and_metadata_differences_are_ignored(tmp_path: Path) -> None:
    """source_path and metadata_json live in documents, not content_json — invisible."""
    conn_a = _fresh_conn(tmp_path, "a.db")
    conn_b = _fresh_conn(tmp_path, "b.db")
    _write_context(conn_a, "proj", "feat", "Hello world")
    _write_context(conn_b, "proj", "feat", "Hello world")
    # Patch DB A to simulate a walker-written row (source_path set, different metadata)
    conn_a.execute(
        "UPDATE documents SET source_path='/docs/proj/feat/context.html', "
        'metadata_json=\'{"title":"feat — Context","size":4096}\''
    )

    report = compare_dbs(conn_a, conn_b)

    assert report.ok
    assert report.mismatches == ()


def test_key_only_in_a_is_reported(tmp_path: Path) -> None:
    conn_a = _fresh_conn(tmp_path, "a.db")
    conn_b = _fresh_conn(tmp_path, "b.db")
    _write_context(conn_a, "proj", "feat", "Hello")

    report = compare_dbs(conn_a, conn_b)

    assert not report.ok
    assert report.only_in_a == ("proj/feat/context/1",)
    assert report.only_in_b == ()
    assert report.mismatches == ()


def test_key_only_in_b_is_reported(tmp_path: Path) -> None:
    conn_a = _fresh_conn(tmp_path, "a.db")
    conn_b = _fresh_conn(tmp_path, "b.db")
    _write_context(conn_b, "proj", "feat", "Hello")

    report = compare_dbs(conn_a, conn_b)

    assert not report.ok
    assert report.only_in_a == ()
    assert report.only_in_b == ("proj/feat/context/1",)
    assert report.mismatches == ()


def test_multiple_docs_all_matching(tmp_path: Path) -> None:
    conn_a = _fresh_conn(tmp_path, "a.db")
    conn_b = _fresh_conn(tmp_path, "b.db")
    for feature in ("feat-a", "feat-b", "feat-c"):
        _write_context(conn_a, "proj", feature, f"content for {feature}")
        _write_context(conn_b, "proj", feature, f"content for {feature}")

    report = compare_dbs(conn_a, conn_b)

    assert report.ok
    assert report.mismatches == ()


def test_multiple_docs_one_mismatch(tmp_path: Path) -> None:
    conn_a = _fresh_conn(tmp_path, "a.db")
    conn_b = _fresh_conn(tmp_path, "b.db")
    _write_context(conn_a, "proj", "feat-a", "Same content")
    _write_context(conn_b, "proj", "feat-a", "Same content")
    _write_context(conn_a, "proj", "feat-b", "Old content")
    _write_context(conn_b, "proj", "feat-b", "New content")

    report = compare_dbs(conn_a, conn_b)

    assert not report.ok
    assert len(report.mismatches) == 1
    assert report.mismatches[0].logical_key == "proj/feat-b/context/1"
