from pathlib import Path

from starlette.testclient import TestClient

from feature_skills_webapp.web.app import create_app
from feature_skills_webapp.web.routes import MARKER

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="{doc_type}">
<title>{title}</title>
</head>
<body></body>
</html>
"""


def make_docs_root(tmp_path: Path) -> Path:
    docs_root = tmp_path / "docs"
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(
        HTML_TEMPLATE.format(doc_type="context", title="ctx")
    )
    return docs_root


def test_index_returns_200() -> None:
    client = TestClient(create_app(db_path=None))
    response = client.get("/")
    assert response.status_code == 200


def test_index_contains_marker() -> None:
    client = TestClient(create_app(db_path=None))
    response = client.get("/")
    assert MARKER in response.text


def test_healthz_ok(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_unavailable_when_db_is_directory(tmp_path: Path) -> None:
    # Point db_path at a directory so sqlite3.connect raises.
    client = TestClient(create_app(db_path=tmp_path), raise_server_exceptions=False)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_admin_discover_returns_summary(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        response = client.post("/admin/discover")
    assert response.status_code == 200
    data = response.json()
    assert "created" in data
    assert "errors" in data
    assert isinstance(data["created"], int)


def test_admin_discover_503_when_unwired(tmp_path: Path) -> None:
    # No docs_root means discovery is not wired up
    client = TestClient(create_app(db_path=None, docs_root=None))
    response = client.post("/admin/discover")
    assert response.status_code == 503


def test_index_still_ok_with_new_lifespan(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        response = client.get("/")
    assert response.status_code == 200


def test_healthz_still_ok_with_docs_root(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_admin_mark_read_returns_summary(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        response = client.post("/admin/projects/proj1/mark-read")
    assert response.status_code == 200
    data = response.json()
    assert data["project"] == "proj1"
    assert isinstance(data["stamped"], int)
    assert data["stamped"] >= 1


def test_admin_mark_read_clears_unread(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        client.post("/admin/projects/proj1/mark-read")

    from feature_skills_webapp.storage.db import connect
    from feature_skills_webapp.storage.read_state import unread_document_ids

    conn = connect(temp_db)
    proj_id = conn.execute("SELECT id FROM projects WHERE name='proj1'").fetchone()["id"]
    assert unread_document_ids(conn, project_id=proj_id) == []
    conn.close()


def test_admin_mark_read_unknown_project_returns_404(temp_db: Path, tmp_path: Path) -> None:
    docs_root = make_docs_root(tmp_path)
    with TestClient(create_app(db_path=temp_db, docs_root=docs_root)) as client:
        response = client.post("/admin/projects/no-such-project/mark-read")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown project"


def test_admin_mark_read_503_when_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    response = client.post("/admin/projects/proj1/mark-read")
    assert response.status_code == 503
    assert response.json()["error"] == "db not configured"
