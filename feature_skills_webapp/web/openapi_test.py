import pytest
from openapi_spec_validator import validate
from starlette.routing import Route
from starlette.testclient import TestClient

from feature_skills_webapp.web.app import create_app
from feature_skills_webapp.web.openapi import API_METADATA, build_spec


def _api_routes() -> list[Route]:
    app = create_app(db_path=None)
    return [r for r in app.routes if isinstance(r, Route) and r.path.startswith("/api")]


def _walked_operations() -> set[tuple[str, str]]:
    """Every (method, path_format) the app actually serves under /api, HEAD/OPTIONS excluded."""
    ops = set()
    for route in _api_routes():
        for method in route.methods or []:
            if method in ("HEAD", "OPTIONS"):
                continue
            ops.add((method, route.path_format))
    return ops


def test_coverage_every_api_operation_has_a_curated_summary() -> None:
    """THE anti-drift guard: a new /api route without API_METADATA fails this test.

    Do not delete as redundant with the parity tests below — build_spec() emits an
    empty-summary operation for undocumented routes, so route<->spec parity alone
    cannot detect a missing summary. This is the only test that can.
    """
    missing = [op for op in _walked_operations() if not API_METADATA.get(op, {}).get("summary")]
    assert missing == []


def test_parity_every_route_appears_in_the_spec() -> None:
    """Complements coverage: every walked route/method is emitted by build_spec()."""
    spec = build_spec(_api_routes(), base_url="http://127.0.0.1:8800", version="0.1.0")
    for method, path_format in _walked_operations():
        assert path_format in spec["paths"]
        assert method.lower() in spec["paths"][path_format]


def test_no_phantom_operations_in_the_spec() -> None:
    """Every operation in the spec maps back to a real route/method."""
    spec = build_spec(_api_routes(), base_url="http://127.0.0.1:8800", version="0.1.0")
    walked = _walked_operations()
    for path_format, path_item in spec["paths"].items():
        for method in path_item:
            assert (method.upper(), path_format) in walked


def test_spec_is_a_valid_openapi_document() -> None:
    spec = build_spec(_api_routes(), base_url="http://127.0.0.1:8800", version="0.1.0")
    validate(spec)  # raises on invalid


def test_openapi_json_endpoint_returns_the_spec() -> None:
    client = TestClient(create_app(db_path=None))
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert body["openapi"] == "3.1.0"


def test_openapi_json_works_without_configured_db() -> None:
    """Unlike /api handlers, /openapi.json describes routes, not data — no 503."""
    client = TestClient(create_app(db_path=None))
    response = client.get("/openapi.json")
    assert response.status_code == 200


def test_servers_uses_public_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_PUBLIC_URL", "https://example.com")
    client = TestClient(create_app(db_path=None))
    response = client.get("/openapi.json")
    assert response.json()["servers"][0]["url"] == "https://example.com"


def test_servers_falls_back_to_loopback_on_wildcard_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FEATURE_SKILLS_WEBAPP_PUBLIC_URL", raising=False)
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_HOST", "0.0.0.0")
    client = TestClient(create_app(db_path=None))
    response = client.get("/openapi.json")
    assert response.json()["servers"][0]["url"].startswith("http://127.0.0.1:")


def test_integer_path_param_typed_as_integer() -> None:
    spec = build_spec(_api_routes(), base_url="http://127.0.0.1:8800", version="0.1.0")
    op = spec["paths"]["/api/documents/{project}/{feature}/{doc_type}/{instance}"]["get"]
    param = next(p for p in op["parameters"] if p["name"] == "instance")
    assert param["schema"]["type"] == "integer"


def test_string_path_param_typed_as_string() -> None:
    spec = build_spec(_api_routes(), base_url="http://127.0.0.1:8800", version="0.1.0")
    op = spec["paths"]["/api/projects/{project}"]["get"]
    param = next(p for p in op["parameters"] if p["name"] == "project")
    assert param["schema"]["type"] == "string"


def test_info_version_is_a_non_empty_string() -> None:
    """Not hard-coded: differs between an installed tree and a bare checkout."""
    client = TestClient(create_app(db_path=None))
    response = client.get("/openapi.json")
    version = response.json()["info"]["version"]
    assert isinstance(version, str)
    assert version != ""


def test_head_and_options_are_not_emitted_as_operations() -> None:
    spec = build_spec(_api_routes(), base_url="http://127.0.0.1:8800", version="0.1.0")
    for path_item in spec["paths"].values():
        assert "head" not in path_item
        assert "options" not in path_item


def test_multi_method_path_merges_into_one_path_item() -> None:
    """GET+POST on /api/projects/{project} share one path-item with two operations."""
    spec = build_spec(_api_routes(), base_url="http://127.0.0.1:8800", version="0.1.0")
    path_item = spec["paths"]["/api/projects/{project}"]
    assert "get" in path_item
    assert "post" in path_item
