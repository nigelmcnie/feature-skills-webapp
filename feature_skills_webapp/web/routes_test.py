from starlette.testclient import TestClient

from feature_skills_webapp.web.app import create_app
from feature_skills_webapp.web.routes import MARKER


def test_index_returns_200():
    client = TestClient(create_app(db_path=None))
    response = client.get("/")
    assert response.status_code == 200


def test_index_contains_marker():
    client = TestClient(create_app(db_path=None))
    response = client.get("/")
    assert MARKER in response.text
