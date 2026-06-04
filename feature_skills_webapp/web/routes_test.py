from pathlib import Path

from starlette.testclient import TestClient

from feature_skills_webapp.web.app import create_app
from feature_skills_webapp.web.routes import MARKER


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
