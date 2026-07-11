from pathlib import Path

import pytest
from openapi_spec_validator import validate
from starlette.requests import Request
from starlette.responses import JSONResponse
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


# --- Phase 2: curated request/response detail ---

_HIGH_VALUE_OPS_WITH_REQUEST_BODIES = {
    ("PUT", "/api/documents/{project}/{feature}/{doc_type}/{instance}"),
    ("POST", "/api/documents/{project}/{feature}/{doc_type}/{instance}/comments/integrate"),
    ("POST", "/api/documents/{project}/{feature}/{doc_type}/{instance}/archive"),
    ("POST", "/api/documents/{project}/{feature}/{doc_type}/{instance}/unarchive"),
    ("PUT", "/api/projects/{project}/suggested-order"),
    ("POST", "/api/projects/{project}/features/{feature}"),
    ("POST", "/api/projects/{project}/features/{feature}/claim"),
    ("POST", "/api/projects/{project}/features/{feature}/ship"),
    ("POST", "/api/projects/{project}/features/{feature}/archive"),
    ("POST", "/api/projects/{project}/features/{feature}/note"),
}


def _spec() -> dict:
    return build_spec(_api_routes(), base_url="http://127.0.0.1:8800", version="0.1.0")


def test_high_value_operations_declare_a_request_body() -> None:
    spec = _spec()
    for method, path_format in _HIGH_VALUE_OPS_WITH_REQUEST_BODIES:
        op = spec["paths"][path_format][method.lower()]
        assert "requestBody" in op, f"{method} {path_format} missing requestBody"


def test_high_value_operations_declare_error_responses() -> None:
    """Every operation with meaningful failure modes documents them, not just 200."""
    spec = _spec()
    for method, path_format in _HIGH_VALUE_OPS_WITH_REQUEST_BODIES:
        responses = spec["paths"][path_format][method.lower()]["responses"]
        assert "503" in responses
        assert any(code in responses for code in ("400", "404", "409"))


def test_document_write_has_dry_run_query_param() -> None:
    spec = _spec()
    op = spec["paths"]["/api/documents/{project}/{feature}/{doc_type}/{instance}"]["put"]
    names = {p["name"] for p in op["parameters"]}
    assert "dry_run" in names


def test_features_listing_has_q_and_status_query_params() -> None:
    spec = _spec()
    op = spec["paths"]["/api/projects/{project}/features"]["get"]
    names = {p["name"] for p in op["parameters"]}
    assert {"q", "status"} <= names


def test_documents_listing_has_status_query_param() -> None:
    spec = _spec()
    op = spec["paths"]["/api/projects/{project}/features/{feature}/documents"]["get"]
    names = {p["name"] for p in op["parameters"]}
    assert "status" in names


def test_archive_operation_has_reason_enum_and_error_responses() -> None:
    spec = _spec()
    op = spec["paths"]["/api/documents/{project}/{feature}/{doc_type}/{instance}/archive"]["post"]
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]["reason"]["enum"]) == {"superseded", "duplicate", "obsolete"}
    assert schema["required"] == ["reason"]
    for code in ("400", "404", "409"):
        assert code in op["responses"]


def test_unarchive_operation_has_error_responses() -> None:
    spec = _spec()
    op = spec["paths"]["/api/documents/{project}/{feature}/{doc_type}/{instance}/unarchive"]["post"]
    for code in ("404", "409"):
        assert code in op["responses"]


def test_feature_sentinel_documented_on_every_document_path() -> None:
    """Every /api/documents/.../{feature}/... path explains the '-' sentinel."""
    spec = _spec()
    for path_format, path_item in spec["paths"].items():
        if not path_format.startswith("/api/documents/"):
            continue
        for op in path_item.values():
            feature_param = next(p for p in op["parameters"] if p["name"] == "feature")
            assert "-" in feature_param["description"]


def test_document_write_schema_points_to_the_manifest_endpoint() -> None:
    """Section shapes aren't re-described inline — they point at the source of truth."""
    op = _spec()["paths"]["/api/documents/{project}/{feature}/{doc_type}/{instance}"]["put"]
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert "/api/manifests/" in schema["properties"]["sections"]["description"]


def test_error_responses_reference_the_shared_error_schema() -> None:
    op = _spec()["paths"]["/api/projects/{project}"]["get"]
    error_response = op["responses"]["404"]
    schema = error_response["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/Error"}


def test_components_error_schema_is_a_valid_object_schema() -> None:
    spec = _spec()
    error_schema = spec["components"]["schemas"]["Error"]
    assert error_schema["type"] == "object"
    assert "error" in error_schema["properties"]


def test_get_manifest_has_no_db_dependent_error_responses() -> None:
    """get_manifest never touches the DB, so 503 would misdocument its real behaviour."""
    op = _spec()["paths"]["/api/manifests/{doc_type}"]["get"]
    assert "503" not in op.get("responses", {})


# --- Golden-response tests: curated examples pinned against real responses ---


def test_golden_response_list_projects_matches_curated_example(temp_db: Path) -> None:
    example = _spec()["paths"]["/api/projects"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["example"]
    with TestClient(create_app(db_path=temp_db)) as client:
        response = client.get("/api/projects")
    assert set(response.json().keys()) == set(example.keys())


def test_golden_response_create_project_matches_curated_example(temp_db: Path) -> None:
    example = _spec()["paths"]["/api/projects/{project}"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["example"]
    with TestClient(create_app(db_path=temp_db)) as client:
        response = client.post("/api/projects/my-proj")
    assert set(response.json().keys()) == set(example.keys())


def test_golden_response_claim_feature_matches_curated_example(temp_db: Path) -> None:
    example = _spec()["paths"]["/api/projects/{project}/features/{feature}/claim"]["post"][
        "responses"
    ]["200"]["content"]["application/json"]["example"]
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/my-proj")
        client.post("/api/projects/my-proj/features/my-feat", json={"notes": ""})
        response = client.post(
            "/api/projects/my-proj/features/my-feat/claim", json={"owner": "Alice"}
        )
    assert set(response.json().keys()) == set(example.keys())


def test_golden_response_get_manifest_matches_curated_example() -> None:
    example = _spec()["paths"]["/api/manifests/{doc_type}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["example"]
    client = TestClient(create_app(db_path=None))
    response = client.get("/api/manifests/plan")
    assert set(response.json().keys()) == set(example.keys())


_DOC_URL = "/api/documents/my-proj/my-feat/requirements/1"


def _seed_document(client: TestClient) -> None:
    client.post("/api/projects/my-proj")
    client.post("/api/projects/my-proj/features/my-feat", json={"notes": ""})
    client.put(_DOC_URL, json={"sections": {"summary": "<p>x</p>"}})


def test_golden_response_document_put_matches_curated_example(temp_db: Path) -> None:
    """The document PUT is the feature's highest-value op — pin its curated example."""
    example = _spec()["paths"]["/api/documents/{project}/{feature}/{doc_type}/{instance}"]["put"][
        "responses"
    ]["200"]["content"]["application/json"]["example"]
    with TestClient(create_app(db_path=temp_db)) as client:
        client.post("/api/projects/my-proj")
        client.post("/api/projects/my-proj/features/my-feat", json={"notes": ""})
        response = client.put(_DOC_URL, json={"sections": {"summary": "<p>x</p>"}})
    assert response.status_code == 200
    assert set(response.json().keys()) == set(example.keys())


def test_golden_response_document_get_matches_curated_example(temp_db: Path) -> None:
    example = _spec()["paths"]["/api/documents/{project}/{feature}/{doc_type}/{instance}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["example"]
    with TestClient(create_app(db_path=temp_db)) as client:
        _seed_document(client)
        response = client.get(_DOC_URL)
    assert response.status_code == 200
    assert set(response.json().keys()) == set(example.keys())


def test_coverage_guard_catches_an_undocumented_route() -> None:
    """The guard's own red path, encoded rather than checked by hand: an /api route
    absent from API_METADATA is flagged by coverage even though build_spec still emits
    it (with an empty summary), so route<->spec parity would stay green."""

    async def _dummy(request: Request) -> JSONResponse:  # pragma: no cover - never called
        return JSONResponse({})

    routes = [*_api_routes(), Route("/api/dummy-undocumented", _dummy, methods=["GET"])]
    spec = build_spec(routes, base_url="http://127.0.0.1:8800", version="0.1.0")
    # Parity would NOT catch it — the operation is emitted, just with an empty summary.
    assert spec["paths"]["/api/dummy-undocumented"]["get"].get("summary", "") == ""
    # Coverage WOULD catch it — the guard predicate flags the missing summary.
    walked = {
        (m, r.path_format)
        for r in routes
        for m in (r.methods or [])
        if m not in ("HEAD", "OPTIONS")
    }
    missing = [op for op in walked if not API_METADATA.get(op, {}).get("summary")]
    assert ("GET", "/api/dummy-undocumented") in missing
