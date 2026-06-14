"""Tests for web/retro_findings.py."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from starlette.applications import Starlette
from starlette.testclient import TestClient

from feature_skills_webapp.storage.db import connect, transaction
from feature_skills_webapp.web.app import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_RUN = {"key": "feat-2026-06-14T22:00Z", "feature": "some-feat"}
_BASE_FINDING = {"title": "Something worth noting"}


def _post_run(
    client: TestClient,
    *,
    project: str = "proj",
    run: dict | None = None,
    findings: list | None = None,
) -> dict:
    payload: dict = {
        "project": project,
        "run": run if run is not None else _BASE_RUN,
        "findings": findings if findings is not None else [_BASE_FINDING],
    }
    resp = client.post("/retro-findings", json=payload)
    return resp.json() | {"status_code": resp.status_code}


def _seed_project(db_path: Path, name: str = "proj") -> int:
    """Insert a project row and return its id."""
    conn = connect(db_path)
    now = "2026-06-14T00:00:00+00:00"
    with transaction(conn):
        conn.execute("INSERT INTO projects (name, created_at) VALUES (?, ?)", (name, now))
    proj_id: int = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()["id"]
    conn.close()
    return proj_id


# ---------------------------------------------------------------------------
# POST /retro-findings
# ---------------------------------------------------------------------------


def test_round_trip_writes_and_reads_findings(temp_db: Path) -> None:
    _seed_project(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        # Write two findings
        resp = client.post(
            "/retro-findings",
            json={
                "project": "proj",
                "run": {"key": "feat-2026-06-14T22:00Z", "feature": "my-feat"},
                "findings": [
                    {"title": "Finding A", "evidence": "e1", "change": "c1"},
                    {"title": "Finding B"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["findings_written"] == 2
        run_id = data["run_id"]
        assert isinstance(run_id, int)

        # Read them back
        get_resp = client.get("/retro-findings?project=proj")
        assert get_resp.status_code == 200
        got = get_resp.json()
        assert got["project"] == "proj"
        findings = got["findings"]
        assert len(findings) == 2
        titles = [f["title"] for f in findings]
        assert "Finding A" in titles
        assert "Finding B" in titles
        for f in findings:
            assert f["status"] == "open"
            assert f["feature"] == "my-feat"
            assert f["recurs_from"] is None
            assert f["recurrence_count"] == 0
            assert "id" in f


def test_idempotent_repost_replaces_findings(temp_db: Path) -> None:
    _seed_project(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        run_key = "feat-k"
        # First post: 3 findings
        client.post(
            "/retro-findings",
            json={
                "project": "proj",
                "run": {"key": run_key},
                "findings": [{"title": "A"}, {"title": "B"}, {"title": "C"}],
            },
        )
        # Re-post same key: 1 finding
        resp = client.post(
            "/retro-findings",
            json={
                "project": "proj",
                "run": {"key": run_key},
                "findings": [{"title": "Only one"}],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["findings_written"] == 1

        get_resp = client.get("/retro-findings?project=proj")
        findings = get_resp.json()["findings"]
        assert len(findings) == 1
        assert findings[0]["title"] == "Only one"


def test_recurrence_round_trip(temp_db: Path) -> None:
    _seed_project(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        # Run A: one finding
        client.post(
            "/retro-findings",
            json={
                "project": "proj",
                "run": {"key": "run-a"},
                "findings": [{"title": "Original finding"}],
            },
        )
        # GET to learn the id
        findings_a = client.get("/retro-findings?project=proj").json()["findings"]
        assert len(findings_a) == 1
        original_id = findings_a[0]["id"]

        # Run B: cites the original
        resp = client.post(
            "/retro-findings",
            json={
                "project": "proj",
                "run": {"key": "run-b"},
                "findings": [{"title": "Recurrence", "recurs_from": original_id}],
            },
        )
        assert resp.status_code == 200

        # GET shows recurrence_count == 1 on original
        findings = client.get("/retro-findings?project=proj").json()["findings"]
        original = next(f for f in findings if f["id"] == original_id)
        assert original["recurrence_count"] == 1


def test_self_run_recurs_from_rejected(temp_db: Path) -> None:
    """A finding citing recurs_from belonging to the run being replaced → 400."""
    _seed_project(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        # First post run K → get an id for one of its findings
        client.post(
            "/retro-findings",
            json={
                "project": "proj",
                "run": {"key": "run-k"},
                "findings": [{"title": "Old finding"}],
            },
        )
        old_id = client.get("/retro-findings?project=proj").json()["findings"][0]["id"]

        # Re-post run K citing the old finding (which belongs to run-k being replaced) → 400
        resp = client.post(
            "/retro-findings",
            json={
                "project": "proj",
                "run": {"key": "run-k"},
                "findings": [{"title": "New", "recurs_from": old_id}],
            },
        )
        assert resp.status_code == 400


def test_cross_project_recurs_from_rejected(temp_db: Path) -> None:
    _seed_project(temp_db, "proj1")
    _seed_project(temp_db, "proj2")
    with TestClient(create_app(db_path=temp_db)) as client:
        # Post a finding in proj1
        client.post(
            "/retro-findings",
            json={
                "project": "proj1",
                "run": {"key": "run-1"},
                "findings": [{"title": "proj1 finding"}],
            },
        )
        proj1_id = client.get("/retro-findings?project=proj1").json()["findings"][0]["id"]

        # Try to cite it from proj2 → 400
        resp = client.post(
            "/retro-findings",
            json={
                "project": "proj2",
                "run": {"key": "run-2"},
                "findings": [{"title": "proj2 finding", "recurs_from": proj1_id}],
            },
        )
        assert resp.status_code == 400


def test_read_filter_excludes_actioned_and_rejected(temp_db: Path) -> None:
    proj_id = _seed_project(temp_db)
    client = TestClient(create_app(db_path=temp_db))

    # Seed findings directly with various statuses
    conn = connect(temp_db)
    now = "2026-01-01T00:00:00+00:00"
    with transaction(conn):
        conn.execute(
            "INSERT INTO retro_runs (project_id, run_key, created_at) VALUES (?, 'r1', ?)",
            (proj_id, now),
        )
        run_id = conn.execute("SELECT id FROM retro_runs WHERE run_key = 'r1'").fetchone()["id"]
        for title, status in [
            ("open finding", "open"),
            ("deferred finding", "deferred"),
            ("actioned finding", "actioned"),
            ("rejected finding", "rejected"),
        ]:
            conn.execute(
                "INSERT INTO retro_findings "
                "(run_id, project_id, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, proj_id, title, status, now, now),
            )
    conn.close()

    get_resp = client.get("/retro-findings?project=proj")
    assert get_resp.status_code == 200
    titles = [f["title"] for f in get_resp.json()["findings"]]
    assert "open finding" in titles
    assert "deferred finding" in titles
    assert "actioned finding" not in titles
    assert "rejected finding" not in titles


def test_post_404_unknown_project(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/retro-findings",
        json={"project": "no-such", "run": {"key": "k"}, "findings": []},
    )
    assert resp.status_code == 404


def test_get_404_unknown_project(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.get("/retro-findings?project=no-such")
    assert resp.status_code == 404


def test_get_400_missing_project_param(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.get("/retro-findings")
    assert resp.status_code == 400


def test_post_400_missing_project(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/retro-findings", json={"run": {"key": "k"}, "findings": []})
    assert resp.status_code == 400


def test_post_400_missing_run_key(temp_db: Path) -> None:
    _seed_project(temp_db)
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/retro-findings",
        json={"project": "proj", "run": {"feature": "x"}, "findings": []},
    )
    assert resp.status_code == 400


def test_post_400_empty_run_key(temp_db: Path) -> None:
    _seed_project(temp_db)
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/retro-findings",
        json={"project": "proj", "run": {"key": ""}, "findings": []},
    )
    assert resp.status_code == 400


def test_post_400_empty_finding_title(temp_db: Path) -> None:
    _seed_project(temp_db)
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/retro-findings",
        json={
            "project": "proj",
            "run": {"key": "k"},
            "findings": [{"title": ""}],
        },
    )
    assert resp.status_code == 400


def test_post_400_findings_not_list(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/retro-findings",
        json={"project": "proj", "run": {"key": "k"}, "findings": "bad"},
    )
    assert resp.status_code == 400


def test_post_400_oversize_field(temp_db: Path) -> None:
    _seed_project(temp_db)
    client = TestClient(create_app(db_path=temp_db))
    big = "x" * (1024 * 1024 + 1)
    resp = client.post(
        "/retro-findings",
        json={
            "project": "proj",
            "run": {"key": "k"},
            "findings": [{"title": big}],
        },
    )
    assert resp.status_code == 400


def test_post_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post(
        "/retro-findings",
        json={"project": "p", "run": {"key": "k"}, "findings": []},
    )
    assert resp.status_code == 503


def test_get_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get("/retro-findings?project=p")
    assert resp.status_code == 503


def test_post_broadcasts(temp_db: Path) -> None:
    _seed_project(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        app = cast(Starlette, client.app)
        q = app.state.broadcaster.register()
        client.post(
            "/retro-findings",
            json={
                "project": "proj",
                "run": {"key": "k"},
                "findings": [{"title": "Broadcast test"}],
            },
        )
        assert not q.empty()
        app.state.broadcaster.unregister(q)


def test_feature_copied_from_run_into_findings(temp_db: Path) -> None:
    _seed_project(temp_db)
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post(
            "/retro-findings",
            json={
                "project": "proj",
                "run": {"key": "k", "feature": "my-feature"},
                "findings": [{"title": "A"}, {"title": "B"}],
            },
        )
        findings = client.get("/retro-findings?project=proj").json()["findings"]
        for f in findings:
            assert f["feature"] == "my-feature"


def test_recurs_from_nonexistent_finding_rejected(temp_db: Path) -> None:
    _seed_project(temp_db)
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post(
        "/retro-findings",
        json={
            "project": "proj",
            "run": {"key": "k"},
            "findings": [{"title": "A", "recurs_from": 99999}],
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /retro-findings/{id}/status  (Phase 2)
# ---------------------------------------------------------------------------


def _seed_finding(db_path: Path, proj_id: int, *, status: str = "open") -> int:
    """Directly insert a run + finding; return the finding id."""
    conn = connect(db_path)
    now = "2026-06-14T00:00:00+00:00"
    with transaction(conn):
        conn.execute(
            "INSERT INTO retro_runs (project_id, run_key, created_at) VALUES (?, 'seed-run', ?)",
            (proj_id, now),
        )
        run_id: int = conn.execute(
            "SELECT id FROM retro_runs WHERE run_key='seed-run' AND project_id=?", (proj_id,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO retro_findings "
            "(run_id, project_id, title, status, created_at, updated_at) "
            "VALUES (?, ?, 'Seeded finding', ?, ?, ?)",
            (run_id, proj_id, status, now, now),
        )
        fid: int = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return fid


def test_status_update_happy_path(temp_db: Path) -> None:
    proj_id = _seed_project(temp_db)
    fid = _seed_finding(temp_db, proj_id)
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(f"/retro-findings/{fid}/status", json={"status": "actioned"})
    assert resp.status_code == 200
    assert resp.json() == {"id": fid, "status": "actioned"}


def test_status_update_excluded_from_get_filter(temp_db: Path) -> None:
    proj_id = _seed_project(temp_db)
    fid = _seed_finding(temp_db, proj_id)
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post(f"/retro-findings/{fid}/status", json={"status": "actioned"})
        findings = client.get("/retro-findings?project=proj").json()["findings"]
    assert not any(f["id"] == fid for f in findings)


def test_status_update_writes_events_row(temp_db: Path) -> None:
    proj_id = _seed_project(temp_db)
    fid = _seed_finding(temp_db, proj_id, status="open")
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post(f"/retro-findings/{fid}/status", json={"status": "deferred"})

    conn = connect(temp_db)
    row = conn.execute(
        "SELECT event_type, payload_json FROM events "
        "WHERE event_type = 'retro_finding_status_changed' LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    import json as _json

    payload = _json.loads(row["payload_json"])
    assert payload["finding_id"] == fid
    assert payload["old_status"] == "open"
    assert payload["new_status"] == "deferred"


def test_status_noop_writes_no_event(temp_db: Path) -> None:
    proj_id = _seed_project(temp_db)
    fid = _seed_finding(temp_db, proj_id, status="open")
    with TestClient(create_app(db_path=temp_db)) as client:
        resp = client.post(f"/retro-findings/{fid}/status", json={"status": "open"})
    assert resp.status_code == 200

    conn = connect(temp_db)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type='retro_finding_status_changed'"
    ).fetchone()["n"]
    conn.close()
    assert count == 0


def test_status_404_unknown_finding(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/retro-findings/99999/status", json={"status": "open"})
    assert resp.status_code == 404


def test_status_400_invalid_status(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.post("/retro-findings/1/status", json={"status": "invalid"})
    assert resp.status_code == 400


def test_status_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.post("/retro-findings/1/status", json={"status": "open"})
    assert resp.status_code == 503


def test_status_broadcasts_on_real_change(temp_db: Path) -> None:
    proj_id = _seed_project(temp_db)
    fid = _seed_finding(temp_db, proj_id)
    with TestClient(create_app(db_path=temp_db)) as client:
        app = cast(Starlette, client.app)
        q = app.state.broadcaster.register()
        client.post(f"/retro-findings/{fid}/status", json={"status": "actioned"})
        assert not q.empty()
        app.state.broadcaster.unregister(q)


def test_status_does_not_broadcast_on_noop(temp_db: Path) -> None:
    proj_id = _seed_project(temp_db)
    fid = _seed_finding(temp_db, proj_id, status="open")
    with TestClient(create_app(db_path=temp_db)) as client:
        app = cast(Starlette, client.app)
        q = app.state.broadcaster.register()
        client.post(f"/retro-findings/{fid}/status", json={"status": "open"})
        assert q.empty()
        app.state.broadcaster.unregister(q)


# ---------------------------------------------------------------------------
# Project page — Process findings panel  (Phase 2)
# ---------------------------------------------------------------------------


def test_project_page_shows_finding_title(temp_db: Path, tmp_path: Path) -> None:
    from feature_skills_webapp.storage.db import connect as db_connect

    docs_root = tmp_path / "docs"
    (docs_root / "proj" / "feat-a").mkdir(parents=True)
    (docs_root / "proj" / "feat-a" / "context.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="feature-doc-type" content="context"><title>ctx</title>'
        "</head><body>ctx</body></html>"
    )
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        conn = db_connect(temp_db)
        proj_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
        now = "2026-01-01T00:00:00+00:00"
        with transaction(conn):
            conn.execute(
                "INSERT INTO retro_runs (project_id, run_key, created_at) VALUES (?, 'r', ?)",
                (proj_id, now),
            )
            run_id = conn.execute("SELECT id FROM retro_runs WHERE run_key='r'").fetchone()["id"]
            conn.execute(
                "INSERT INTO retro_findings "
                "(run_id, project_id, title, status, created_at, updated_at) "
                "VALUES (?, ?, 'My important finding', 'open', ?, ?)",
                (run_id, proj_id, now, now),
            )
        conn.close()
        resp = client.get("/project/proj")
    assert resp.status_code == 200
    assert "My important finding" in resp.text
    assert "Process findings" in resp.text


def test_project_page_shows_recurrence_badge(temp_db: Path, tmp_path: Path) -> None:
    from feature_skills_webapp.storage.db import connect as db_connect

    docs_root = tmp_path / "docs"
    (docs_root / "proj" / "feat-a").mkdir(parents=True)
    (docs_root / "proj" / "feat-a" / "context.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="feature-doc-type" content="context"><title>ctx</title>'
        "</head><body>ctx</body></html>"
    )
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/discover")
        conn = db_connect(temp_db)
        proj_id = conn.execute("SELECT id FROM projects WHERE name='proj'").fetchone()["id"]
        now = "2026-01-01T00:00:00+00:00"
        with transaction(conn):
            conn.execute(
                "INSERT INTO retro_runs (project_id, run_key, created_at) VALUES (?, 'r1', ?)",
                (proj_id, now),
            )
            run1_id = conn.execute("SELECT id FROM retro_runs WHERE run_key='r1'").fetchone()["id"]
            conn.execute(
                "INSERT INTO retro_findings "
                "(run_id, project_id, title, status, created_at, updated_at) "
                "VALUES (?, ?, 'Original', 'open', ?, ?)",
                (run1_id, proj_id, now, now),
            )
            parent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO retro_runs (project_id, run_key, created_at) VALUES (?, 'r2', ?)",
                (proj_id, now),
            )
            run2_id = conn.execute("SELECT id FROM retro_runs WHERE run_key='r2'").fetchone()["id"]
            conn.execute(
                "INSERT INTO retro_findings "
                "(run_id, project_id, title, status, recurs_from, created_at, updated_at) "
                "VALUES (?, ?, 'Recurring', 'open', ?, ?, ?)",
                (run2_id, proj_id, parent_id, now, now),
            )
        conn.close()
        resp = client.get("/project/proj")
    assert resp.status_code == 200
    assert "finding-badge" in resp.text
