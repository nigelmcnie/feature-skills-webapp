from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from starlette.convertors import Convertor, FloatConvertor, IntegerConvertor
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

OPENAPI_VERSION = "3.1.0"

API_DESCRIPTION = (
    "Localhost-only, no-auth, single-machine service. Not intended to be exposed "
    "beyond a trusted network."
)

# Curated per-operation metadata, keyed by (HTTP method, Starlette path_format).
# Phase 1 requires only "summary"; phase 2 adds parameters/requestBody/responses.
API_METADATA: dict[tuple[str, str], dict[str, Any]] = {
    ("PUT", "/api/documents/{project}/{feature}/{doc_type}/{instance}"): {
        "summary": "Write a document's content"
    },
    ("GET", "/api/documents/{project}/{feature}/{doc_type}/{instance}"): {
        "summary": "Fetch a document's content"
    },
    ("GET", "/api/documents/{project}/{feature}/{doc_type}/{instance}/comments"): {
        "summary": "List a document's inline comments"
    },
    ("POST", "/api/documents/{project}/{feature}/{doc_type}/{instance}/comments/integrate"): {
        "summary": "Mark a document's comments as integrated"
    },
    ("GET", "/api/documents/{project}/{feature}/{doc_type}/{instance}/synthesis"): {
        "summary": "Fetch a document's synthesis response"
    },
    ("GET", "/api/documents/{project}/{feature}/{doc_type}/{instance}/synthesis/wait"): {
        "summary": "Long-poll for a document's synthesis response"
    },
    ("GET", "/api/manifests/{doc_type}"): {
        "summary": "Fetch the section manifest for a document type"
    },
    ("GET", "/api/projects"): {"summary": "List all projects"},
    ("GET", "/api/projects/{project}"): {"summary": "Fetch a project"},
    ("POST", "/api/projects/{project}"): {"summary": "Create a project"},
    ("PUT", "/api/projects/{project}/suggested-order"): {
        "summary": "Set a project's suggested feature order"
    },
    ("GET", "/api/projects/{project}/features"): {"summary": "List a project's features"},
    ("GET", "/api/projects/{project}/features/{feature}"): {"summary": "Fetch a feature"},
    ("POST", "/api/projects/{project}/features/{feature}"): {"summary": "Create a feature"},
    ("GET", "/api/projects/{project}/features/{feature}/documents"): {
        "summary": "List a feature's documents"
    },
    ("POST", "/api/projects/{project}/features/{feature}/claim"): {"summary": "Claim a feature"},
    ("POST", "/api/projects/{project}/features/{feature}/park"): {"summary": "Park a feature"},
    ("POST", "/api/projects/{project}/features/{feature}/release"): {
        "summary": "Release a claimed feature"
    },
    ("POST", "/api/projects/{project}/features/{feature}/ship"): {"summary": "Ship a feature"},
    ("POST", "/api/projects/{project}/features/{feature}/drop"): {"summary": "Drop a feature"},
    ("POST", "/api/projects/{project}/features/{feature}/note"): {
        "summary": "Add a note to a feature"
    },
}


def _param_type(convertor: Convertor) -> str:
    if isinstance(convertor, IntegerConvertor):
        return "integer"
    if isinstance(convertor, FloatConvertor):
        return "number"
    return "string"  # StringConvertor, PathConvertor, UUIDConvertor, …


def _api_version() -> str:
    try:
        return _pkg_version("feature-skills-webapp")
    except PackageNotFoundError:
        return "0.0.0"


def build_spec(routes: list[Any], *, base_url: str, version: str) -> dict[str, Any]:
    """Assemble the OpenAPI 3.1 document from the live route table + curated metadata."""
    paths: dict[str, dict[str, Any]] = {}
    for route in routes:
        if not isinstance(route, Route) or not route.path.startswith("/api"):
            continue
        methods = {m for m in (route.methods or []) if m not in ("HEAD", "OPTIONS")}
        parameters = [
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": _param_type(convertor)},
            }
            for name, convertor in route.param_convertors.items()
        ]
        path_item = paths.setdefault(route.path_format, {})
        for method in methods:
            meta = API_METADATA.get((method, route.path_format), {})
            operation: dict[str, Any] = {"summary": meta.get("summary", "")}
            if parameters:
                operation["parameters"] = parameters
            path_item[method.lower()] = operation

    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "feature-skills-webapp API",
            "version": version,
            "description": API_DESCRIPTION,
        },
        "servers": [
            {"url": base_url, "description": "Configured public base URL (default: localhost bind)"}
        ],
        "paths": paths,
    }


async def openapi_json(request: Request) -> JSONResponse:
    from feature_skills_webapp.config import public_base_url

    spec = build_spec(
        request.app.routes,
        base_url=public_base_url(),
        version=_api_version(),
    )
    return JSONResponse(spec)
