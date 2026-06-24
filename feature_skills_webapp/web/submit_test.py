"""Tests for web/submit.py (PUT write path + read round-trips by logical identity)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from starlette.applications import Starlette
from starlette.testclient import TestClient

from feature_skills_webapp.web.app import create_app

_PUT_URL = "/api/documents/proj/feat-a/requirements/1"
_VALID_BODY = {"sections": {"problem": "<p>The problem.</p>"}}


# --- happy path ---


def test_put_happy_path_returns_all_fields(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put(_PUT_URL, json=_VALID_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] is True
    assert data["changed"] is True
    assert data["version_num"] == 1
    assert data["logical_key"] == "proj/feat-a/requirements/1"
    assert "document_id" in data
    assert data["url"] == f"/doc/{data['document_id']}"


def test_put_doc_fetchable_at_url(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put(_PUT_URL, json=_VALID_BODY)
        doc_id = resp.json()["document_id"]
        doc_resp = client.get(f"/doc/{doc_id}")
    assert doc_resp.status_code == 200


def test_put_second_submit_with_change(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.put(_PUT_URL, json=_VALID_BODY)
        resp = client.put(_PUT_URL, json={"sections": {"problem": "<p>Updated.</p>"}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] is False
    assert data["changed"] is True
    assert data["version_num"] == 2


def test_put_identical_resubmit_no_change(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.put(_PUT_URL, json=_VALID_BODY)
        resp = client.put(_PUT_URL, json=_VALID_BODY)
    data = resp.json()
    assert data["changed"] is False
    assert data["version_num"] == 1


def test_put_actor_defaults_to_agent(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put(_PUT_URL, json=_VALID_BODY)
    assert resp.status_code == 200


def test_put_custom_actor(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.put(_PUT_URL, json={**_VALID_BODY, "actor": "codex"})
    assert resp.status_code == 200


def test_put_400_non_string_actor(temp_db: Path) -> None:
    # A non-string actor must be rejected, not silently coerced (every other
    # body field here is type-validated).
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL, json={**_VALID_BODY, "actor": 123})
    assert resp.status_code == 400
    assert "actor" in resp.json()["error"]


# --- dry-run ---
# dry_run returns before hitting broadcaster, so no lifespan needed


def test_dry_run_returns_valid_true(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL + "?dry_run=true", json=_VALID_BODY)
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


def test_dry_run_writes_nothing(temp_db: Path) -> None:
    from feature_skills_webapp.storage.db import connect

    client = TestClient(create_app(db_path=temp_db))
    client.put(_PUT_URL + "?dry_run=true", json=_VALID_BODY)

    conn = connect(temp_db)
    row = conn.execute(
        "SELECT id FROM documents WHERE logical_key='proj/feat-a/requirements/1'"
    ).fetchone()
    conn.close()
    assert row is None


def test_dry_run_true_1_value(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL + "?dry_run=1", json=_VALID_BODY)
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


# --- 400 errors ---
# These all return before hitting the broadcaster


def test_put_400_bad_json(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL, content=b"not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_put_400_body_not_dict(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL, json=["not", "a", "dict"])
    assert resp.status_code == 400


def test_put_400_unknown_section_key(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put(_PUT_URL, json={"sections": {"made-up-key": "<p>x</p>"}})
    assert resp.status_code == 400
    assert "unknown section key" in resp.json()["error"]


def test_put_400_non_writable_features_type(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put("/api/documents/proj/feat-a/features/1", json={"sections": {}})
    assert resp.status_code == 400
    assert "not writable" in resp.json()["error"]


def test_put_400_feature_dash_maps_to_none(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put("/api/documents/proj/-/requirements/1", json=_VALID_BODY)
    assert resp.status_code == 400
    assert "feature must be specified" in resp.json()["error"]


def test_put_400_instance_2_for_non_feedback(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.put("/api/documents/proj/feat-a/requirements/2", json=_VALID_BODY)
    assert resp.status_code == 400
    assert "instance must be 1" in resp.json()["error"]


# --- 503 ---


def test_put_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.put(_PUT_URL, json=_VALID_BODY)
    assert resp.status_code == 503
    assert "db not configured" in resp.json()["error"]


# --- broadcast ---


def test_put_broadcasts(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        app = cast(Starlette, client.app)
        q = app.state.broadcaster.register()
        client.put(_PUT_URL, json=_VALID_BODY)
        assert not q.empty()
        app.state.broadcaster.unregister(q)


# ---------------------------------------------------------------------------
# Phase 2: GET /api/documents/{…}  — content read
# ---------------------------------------------------------------------------

_GET_URL = "/api/documents/proj/feat-a/requirements/1"


def test_get_document_round_trips_content(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.put(_PUT_URL, json={"sections": {"scope": "<p>Sc.</p>", "problem": "<p>Pr.</p>"}})
        resp = client.get(_GET_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert data["logical_key"] == "proj/feat-a/requirements/1"
    assert data["doc_type"] == "requirements"
    assert data["shape"] == "sections"
    assert data["version_num"] == 1
    assert "document_id" in data
    assert data["url"] == f"/doc/{data['document_id']}"
    # sections returned in manifest order (problem before scope)
    keys = [s["key"] for s in data["sections"]]
    assert keys == ["problem", "scope"]


def test_get_document_sections_in_manifest_order(temp_db: Path) -> None:
    # Submit scope before problem; GET must return problem first (manifest order)
    with TestClient(create_app(db_path=temp_db)) as client:
        client.put(_PUT_URL, json={"sections": {"scope": "<p>S</p>", "problem": "<p>P</p>"}})
        resp = client.get(_GET_URL)
    keys = [s["key"] for s in resp.json()["sections"]]
    assert keys.index("problem") < keys.index("scope")


def test_get_document_404_unknown_key(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.get(_GET_URL)
    assert resp.status_code == 404


def test_get_document_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get(_GET_URL)
    assert resp.status_code == 503


def test_put_get_round_trip_opaque_feedback(temp_db: Path) -> None:
    # Feedback is opaque (single body) and the only type with instance > 1 —
    # exercises both the opaque write path and the opaque GET branch end-to-end.
    url = "/api/documents/proj/feat-a/requirements-feedback/2"
    with TestClient(create_app(db_path=temp_db)) as client:
        put_resp = client.put(url, json={"body": "<p>feedback body</p>"})
        assert put_resp.status_code == 200
        assert put_resp.json()["created"] is True
        resp = client.get(url)
    assert resp.status_code == 200
    data = resp.json()
    assert data["doc_type"] == "requirements-feedback"
    assert data["shape"] == "opaque"
    assert len(data["sections"]) == 1
    assert data["sections"][0]["body"] == "<p>feedback body</p>"


def test_get_document_project_level_feature_dash(temp_db: Path) -> None:
    # The '-' feature segment maps to None and resolves the project-level
    # logical key 'proj/-/features/1'. Seed such a row directly (no API writer
    # creates project-level docs) and confirm the read path returns it.
    from feature_skills_webapp.storage.db import connect, now_iso
    from feature_skills_webapp.storage.doc_content import ParsedContent, Section
    from feature_skills_webapp.storage.versions import record_version

    with TestClient(create_app(db_path=temp_db)) as client:
        conn = connect(temp_db)
        now = now_iso()
        conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj', ?)", (now,))
        pid = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
        cur = conn.execute(
            "INSERT INTO documents (project_id, feature_id, type, status, logical_key, "
            "instance, created_at, updated_at) "
            "VALUES (?, NULL, 'features', 'active', 'proj/-/features/1', 1, ?, ?)",
            (pid, now, now),
        )
        doc_id = cur.lastrowid
        assert doc_id is not None
        record_version(
            conn,
            doc_id,
            ParsedContent(shape="opaque", sections=(Section(key="", body="<p>tracker</p>"),)),
            actor="importer",
            now=now,
        )
        conn.close()
        resp = client.get("/api/documents/proj/-/features/1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["logical_key"] == "proj/-/features/1"
    assert data["doc_type"] == "features"
    assert data["shape"] == "opaque"
    assert data["sections"][0]["body"] == "<p>tracker</p>"


# ---------------------------------------------------------------------------
# Phase 2: GET /api/manifests/{doc_type}
# ---------------------------------------------------------------------------


def test_get_manifest_section_type() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/manifests/plan")
    assert resp.status_code == 200
    data = resp.json()
    assert data["doc_type"] == "plan"
    assert data["shape"] == "sections"
    sections = data["sections"]
    assert len(sections) > 0
    assert all("key" in s and "label" in s for s in sections)
    assert sections[0]["key"] == "overview"
    assert data["repeated_prefixes"] == ["phase-"]


def test_get_manifest_opaque_type() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/manifests/requirements-feedback")
    assert resp.status_code == 200
    data = resp.json()
    assert data["doc_type"] == "requirements-feedback"
    assert data["shape"] == "opaque"
    assert data["sections"] == []
    assert data["repeated_prefixes"] == []


def test_get_manifest_requirements_has_expected_keys() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/manifests/requirements")
    data = resp.json()
    keys = [s["key"] for s in data["sections"]]
    assert "problem" in keys
    assert "scope" in keys


# ---------------------------------------------------------------------------
# Phase 2: GET /api/documents/{…}/comments + POST …/comments/integrate
# ---------------------------------------------------------------------------

_COMMENTS_URL = "/api/documents/proj/feat-a/requirements/1/comments"
_INTEGRATE_URL = "/api/documents/proj/feat-a/requirements/1/comments/integrate"


def _add_comments(client, doc_id: int, texts: list[str]) -> None:
    client.post(
        f"/doc/{doc_id}/comments",
        json={"comments": [{"text": t} for t in texts]},
    )


def test_get_comments_by_logical_key(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        put_resp = client.put(_PUT_URL, json=_VALID_BODY)
        doc_id = put_resp.json()["document_id"]
        _add_comments(client, doc_id, ["Note A", "Note B"])
        resp = client.get(_COMMENTS_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert data["doc"] == "proj/feat-a/requirements/1"
    assert data["submitted"] is True
    assert len(data["comments"]) == 2
    texts = [c["text"] for c in data["comments"]]
    assert "Note A" in texts
    assert "Note B" in texts


def test_get_comments_empty_before_any_submitted(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.put(_PUT_URL, json=_VALID_BODY)
        resp = client.get(_COMMENTS_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert data["submitted"] is False
    assert data["comments"] == []


def test_integrate_drops_comments_from_active_set(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        put_resp = client.put(_PUT_URL, json=_VALID_BODY)
        doc_id = put_resp.json()["document_id"]
        _add_comments(client, doc_id, ["Keep", "Integrate me"])

        comments = client.get(_COMMENTS_URL).json()["comments"]
        integrate_id = next(c["id"] for c in comments if c["text"] == "Integrate me")

        int_resp = client.post(_INTEGRATE_URL, json={"ids": [integrate_id]})
        assert int_resp.status_code == 200
        assert int_resp.json()["integrated"] == 1

        remaining = client.get(_COMMENTS_URL).json()["comments"]
    assert len(remaining) == 1
    assert remaining[0]["text"] == "Keep"


def test_get_comments_404_unknown_key(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.get(_COMMENTS_URL)
    assert resp.status_code == 404


def test_integrate_404_unknown_key(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(_INTEGRATE_URL, json={"ids": []})
    assert resp.status_code == 404


def test_get_comments_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get(_COMMENTS_URL)
    assert resp.status_code == 503


def test_integrate_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post(_INTEGRATE_URL, json={"ids": []})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Phase 2: GET /api/documents/{…}/synthesis
# ---------------------------------------------------------------------------

_SYNTHESIS_URL = "/api/documents/proj/feat-a/requirements/1/synthesis"


def test_get_synthesis_submitted_false_for_fresh_doc(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        client.put(_PUT_URL, json=_VALID_BODY)
        resp = client.get(_SYNTHESIS_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert data["doc"] == "proj/feat-a/requirements/1"
    assert data["submitted"] is False
    assert data["responses"] == {}
    assert data["routine_flags"] == {}


def test_get_synthesis_populated_after_post(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        put_resp = client.put(_PUT_URL, json=_VALID_BODY)
        doc_id = put_resp.json()["document_id"]
        client.post(
            f"/doc/{doc_id}/synthesis-response",
            json={"responses": {"1": "my answer"}, "routine_flags": {"2": "routine"}},
        )
        resp = client.get(_SYNTHESIS_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert data["submitted"] is True
    assert data["responses"] == {"1": "my answer"}
    assert data["routine_flags"] == {"2": "routine"}


def test_get_synthesis_404_unknown_key(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.get(_SYNTHESIS_URL)
    assert resp.status_code == 404


def test_get_synthesis_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get(_SYNTHESIS_URL)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Presentation contract pointer in manifest
# ---------------------------------------------------------------------------


def test_manifest_has_presentation_stylesheet_url() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/manifests/requirements")
    assert resp.status_code == 200
    data = resp.json()
    assert data["presentation"]["stylesheet_url"] == "/static/doc.css"


def test_manifest_presentation_extra_css_affordance_present() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/api/manifests/plan")
    assert resp.status_code == 200
    data = resp.json()
    assert "extra_css" in data["presentation"]
    assert isinstance(data["presentation"]["extra_css"], str)
    assert len(data["presentation"]["extra_css"]) > 0


def test_manifest_presentation_consistent_across_doc_types() -> None:
    client = TestClient(create_app(db_path=None))
    for doc_type in ("requirements", "plan", "context"):
        resp = client.get(f"/api/manifests/{doc_type}")
        assert resp.status_code == 200
        assert resp.json()["presentation"]["stylesheet_url"] == "/static/doc.css"


def test_static_doc_css_returns_200() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/static/doc.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]
    assert "table {" in resp.text
