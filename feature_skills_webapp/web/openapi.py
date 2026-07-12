from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from starlette.convertors import Convertor, FloatConvertor, IntegerConvertor
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from feature_skills_webapp.config import public_base_url
from feature_skills_webapp.storage.documents import DOC_ARCHIVE_REASONS
from feature_skills_webapp.storage.tracker import ARCHIVE_REASONS

OPENAPI_VERSION = "3.1.0"

API_DESCRIPTION = (
    "Localhost-only, no-auth, single-machine service. Not intended to be exposed "
    "beyond a trusted network."
)

COMPONENTS: dict[str, Any] = {
    "schemas": {
        "Error": {
            "type": "object",
            "properties": {"error": {"type": "string"}},
            "required": ["error"],
        }
    }
}

_ERROR_SCHEMA_REF = {"$ref": "#/components/schemas/Error"}

_MANIFEST_POINTER = (
    'An object mapping each section key to its HTML string ({"<key>": "<html>"}) — '
    "note this differs from the array shape sections are read back as. Section keys are "
    "doc-type specific: fetch GET /api/manifests/{doc_type} for the authoritative list "
    "before writing."
)

_FEATURE_SENTINEL_NOTE = (
    "A value of '-' addresses a project-level document (one with no owning feature) "
    "rather than a real feature slug."
)


def _error(status_description: str, example_message: str) -> dict[str, Any]:
    return {
        "description": status_description,
        "content": {
            "application/json": {
                "schema": _ERROR_SCHEMA_REF,
                "example": {"error": example_message},
            }
        },
    }


def _json_response(description: str, example: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"example": example}},
    }


_DB_NOT_CONFIGURED = _error("Database not configured", "db not configured")
_INVALID_JSON = _error("Request body is not valid JSON", "invalid JSON")


def _changed_body_example(**extra: Any) -> dict[str, Any]:
    return {
        "project": "my-proj",
        "slug": "my-feat",
        "status": "in_progress",
        "changed": True,
        **extra,
    }


def _lifecycle_responses(
    success_example: dict[str, Any],
    *,
    conflict_message: str,
    include_400: bool = False,
) -> dict[str, Any]:
    responses = {
        "200": _json_response("Transition applied (or already in that state)", success_example),
        "404": _error("Feature not found", "feature not found"),
        "409": _error("Invalid state transition", conflict_message),
        "503": _DB_NOT_CONFIGURED,
    }
    if include_400:
        responses["400"] = _INVALID_JSON
    return responses


_DRY_RUN_PARAM = {
    "name": "dry_run",
    "in": "query",
    "required": False,
    "description": "When '1' or 'true', validate the submission without writing it.",
    "schema": {"type": "string"},
}
_Q_PARAM = {
    "name": "q",
    "in": "query",
    "required": False,
    "description": "Case-insensitive substring filter over slug and notes.",
    "schema": {"type": "string"},
}
_STATUS_PARAM = {
    "name": "status",
    "in": "query",
    "required": False,
    "description": "Exact-match filter on feature status.",
    "schema": {"type": "string"},
}
_FEATURE_PATH_PARAM_OVERRIDE = {
    "name": "feature",
    "in": "path",
    "description": _FEATURE_SENTINEL_NOTE,
}
_DOC_LIST_STATUS_PARAM = {
    "name": "status",
    "in": "query",
    "required": False,
    "description": (
        "Filter by document archival status: 'active' (default), 'archived', or 'all'."
    ),
    "schema": {"type": "string", "enum": ["active", "archived", "all"]},
}

# Curated per-operation metadata, keyed by (HTTP method, Starlette path_format).
# Phase 1 requires only "summary"; phase 2 adds parameters/requestBody/responses.
API_METADATA: dict[tuple[str, str], dict[str, Any]] = {
    ("PUT", "/api/documents/{project}/{feature}/{doc_type}/{instance}"): {
        "summary": "Write a document's content",
        "parameters": [_DRY_RUN_PARAM, _FEATURE_PATH_PARAM_OVERRIDE],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "actor": {"type": "string", "description": "Defaults to 'agent'."},
                            "sections": {
                                "type": "object",
                                "description": _MANIFEST_POINTER,
                                "additionalProperties": {"type": "string"},
                            },
                            "body": {
                                "type": "string",
                                "description": "Used instead of 'sections' for opaque-shape doc types.",
                            },
                            "extra_css": {"type": "string"},
                        },
                    }
                }
            },
        },
        "responses": {
            "200": _json_response(
                "Written (or dry-run validated)",
                {
                    "logical_key": "my-proj/my-feat/plan/1",
                    "document_id": 1,
                    "version_num": 1,
                    "url": "/doc/1",
                    "created": True,
                    "changed": True,
                },
            ),
            "400": _error("Body failed validation", "invalid JSON"),
            "404": _error("Project or feature does not exist", "missing feature/project"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("GET", "/api/documents/{project}/{feature}/{doc_type}/{instance}"): {
        "summary": "Fetch a document's content",
        "parameters": [_FEATURE_PATH_PARAM_OVERRIDE],
        "responses": {
            "200": _json_response(
                "Document content",
                {
                    "logical_key": "my-proj/my-feat/plan/1",
                    "document_id": 1,
                    "doc_type": "plan",
                    "shape": "sections",
                    "sections": [{"key": "overview", "body": "<p>…</p>"}],
                    "extra_css": "",
                    "version_num": 1,
                    "url": "/doc/1",
                    "status": "active",
                },
            ),
            "404": _error("Document not found", "document not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("POST", "/api/documents/{project}/{feature}/{doc_type}/{instance}/archive"): {
        "summary": "Archive an API-authored document",
        "parameters": [_FEATURE_PATH_PARAM_OVERRIDE],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "enum": sorted(DOC_ARCHIVE_REASONS),
                                "description": (
                                    "'superseded' and 'duplicate' require 'superseded_by'; "
                                    "'obsolete' may stand alone."
                                ),
                            },
                            "superseded_by": {
                                "type": "string",
                                "description": (
                                    "Free text — a document logical key, an MR, or a "
                                    "decision reference. No resolution required."
                                ),
                            },
                            "note": {"type": "string"},
                            "actor": {"type": "string", "description": "Defaults to 'agent'."},
                        },
                        "required": ["reason"],
                    },
                    "example": {
                        "reason": "superseded",
                        "superseded_by": "proj/feat/vision/1",
                        "note": "content moved to the vision doc",
                        "actor": "agent",
                    },
                }
            },
        },
        "responses": {
            "200": _json_response(
                "Archived (or already archived)",
                {
                    "logical_key": "my-proj/my-feat/requirements/1",
                    "document_id": 1,
                    "status": "archived",
                    "changed": True,
                    "reason": "superseded",
                    "superseded_by": "proj/feat/vision/1",
                    "note": "content moved to the vision doc",
                    "archived_at": "2026-07-12T00:00:00Z",
                },
            ),
            "400": _error(
                "Missing/unknown reason, missing required superseded_by, or self-referential "
                "pointer",
                "'reason' must be one of ['duplicate', 'obsolete', 'superseded']",
            ),
            "404": _error("Document not found", "document not found"),
            "409": _error(
                "Document is file-sourced and not archivable via the API",
                "document is file-sourced and not archivable via the API",
            ),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("POST", "/api/documents/{project}/{feature}/{doc_type}/{instance}/unarchive"): {
        "summary": "Unarchive a previously archived API-authored document",
        "parameters": [_FEATURE_PATH_PARAM_OVERRIDE],
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "actor": {"type": "string", "description": "Defaults to 'agent'."}
                        },
                    }
                }
            },
        },
        "responses": {
            "200": _json_response(
                "Unarchived (or already active)",
                {
                    "logical_key": "my-proj/my-feat/requirements/1",
                    "document_id": 1,
                    "status": "active",
                    "changed": True,
                    "reason": None,
                    "superseded_by": None,
                    "note": None,
                    "archived_at": None,
                },
            ),
            "404": _error("Document not found", "document not found"),
            "409": _error(
                "Document is file-sourced and not archivable via the API",
                "document is file-sourced and not archivable via the API",
            ),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("GET", "/api/documents/{project}/{feature}/{doc_type}/{instance}/comments"): {
        "summary": "List a document's inline comments",
        "parameters": [_FEATURE_PATH_PARAM_OVERRIDE],
        "responses": {
            "200": _json_response(
                "Active comments",
                {"doc": "my-proj/my-feat/plan/1", "submitted": True, "comments": []},
            ),
            "404": _error("Document not found", "document not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("POST", "/api/documents/{project}/{feature}/{doc_type}/{instance}/comments/integrate"): {
        "summary": "Mark a document's comments as integrated",
        "parameters": [_FEATURE_PATH_PARAM_OVERRIDE],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
                        "required": ["ids"],
                    }
                }
            },
        },
        "responses": {
            "200": _json_response("Number of comments integrated", {"integrated": 2}),
            "400": _error("'ids' missing or not a list of integers", "'ids' must be a list"),
            "404": _error("Document not found", "document not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("GET", "/api/documents/{project}/{feature}/{doc_type}/{instance}/synthesis"): {
        "summary": "Fetch a document's synthesis response",
        "parameters": [_FEATURE_PATH_PARAM_OVERRIDE],
        "responses": {
            "200": _json_response(
                "Synthesis state",
                {
                    "doc": "my-proj/my-feat/plan/1",
                    "submitted": False,
                    "responses": {},
                    "routine_flags": {},
                },
            ),
            "404": _error("Document not found", "document not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("GET", "/api/documents/{project}/{feature}/{doc_type}/{instance}/synthesis/wait"): {
        "summary": "Long-poll for a document's synthesis response",
        "parameters": [_FEATURE_PATH_PARAM_OVERRIDE],
        "responses": {
            "200": _json_response(
                "Synthesis state once submitted, or once the wait times out",
                {
                    "doc": "my-proj/my-feat/plan/1",
                    "submitted": True,
                    "responses": {"1": "looks good"},
                    "routine_flags": {},
                },
            ),
            "404": _error("Document not found", "document not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("GET", "/api/manifests/{doc_type}"): {
        "summary": "Fetch the section manifest for a document type",
        "responses": {
            "200": _json_response(
                "Section manifest. No DB required — always available.",
                {
                    "doc_type": "plan",
                    "shape": "sections",
                    "sections": [{"key": "overview", "label": "Overview"}],
                    "repeated_prefixes": [],
                    "presentation": {"stylesheet_url": "/static/doc.css", "extra_css": "…"},
                    "notices": [],
                },
            )
        },
    },
    ("GET", "/api/projects"): {
        "summary": "List all projects",
        "responses": {
            "200": _json_response(
                "All projects", {"projects": [{"name": "my-proj"}], "notices": []}
            ),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("GET", "/api/projects/{project}"): {
        "summary": "Fetch a project",
        "responses": {
            "200": _json_response(
                "Project detail",
                {"project": "my-proj", "repo_path": None, "suggested_order": None},
            ),
            "404": _error("Project not found", "project not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("POST", "/api/projects/{project}"): {
        "summary": "Create a project",
        "responses": {
            "200": _json_response("Project created", {"project": "my-proj"}),
            "409": _error("Project already exists", "project already exists"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("PUT", "/api/projects/{project}/suggested-order"): {
        "summary": "Set a project's suggested feature order",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"text": {"type": ["string", "null"]}},
                    }
                }
            },
        },
        "responses": {
            "200": _json_response(
                "Updated suggested order",
                {"project": "my-proj", "suggested_order": "feat-a, feat-b"},
            ),
            "400": _error("'text' must be a string", "'text' must be a string"),
            "404": _error("Project not found", "project not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("GET", "/api/projects/{project}/features"): {
        "summary": "List a project's features",
        "parameters": [_Q_PARAM, _STATUS_PARAM],
        "responses": {
            "200": _json_response(
                "Matching features",
                {"project": "my-proj", "features": [], "notices": []},
            ),
            "404": _error("Project not found", "project not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("GET", "/api/projects/{project}/features/{feature}"): {
        "summary": "Fetch a feature",
        "responses": {
            "200": _json_response(
                "Feature detail",
                {
                    "project": "my-proj",
                    "slug": "my-feat",
                    "status": "in_progress",
                    "owner": None,
                    "notes": None,
                },
            ),
            "404": _error("Project or feature not found", "feature not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("POST", "/api/projects/{project}/features/{feature}"): {
        "summary": "Create a feature",
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"notes": {"type": ["string", "null"]}},
                    }
                }
            },
        },
        "responses": {
            "200": _json_response(
                "Feature created", _changed_body_example(status="available", changed=True)
            ),
            "404": _error("Project does not exist", "missing project"),
            "409": _error("Feature already exists", "feature already exists"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("GET", "/api/projects/{project}/features/{feature}/documents"): {
        "summary": "List a feature's documents",
        "parameters": [_DOC_LIST_STATUS_PARAM],
        "responses": {
            "200": _json_response(
                "Feature's documents", {"project": "my-proj", "feature": "my-feat", "documents": []}
            ),
            "400": _error(
                "Invalid 'status' value", "'status' must be one of ['active', 'archived', 'all']"
            ),
            "404": _error("Feature not found", "feature not found"),
            "503": _DB_NOT_CONFIGURED,
        },
    },
    ("POST", "/api/projects/{project}/features/{feature}/claim"): {
        "summary": "Claim a feature",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"owner": {"type": "string"}},
                        "required": ["owner"],
                    }
                }
            },
        },
        "responses": _lifecycle_responses(
            _changed_body_example(), conflict_message="invalid transition", include_400=True
        ),
    },
    ("POST", "/api/projects/{project}/features/{feature}/park"): {
        "summary": "Park a feature",
        "responses": _lifecycle_responses(
            _changed_body_example(status="parked"), conflict_message="invalid transition"
        ),
    },
    ("POST", "/api/projects/{project}/features/{feature}/release"): {
        "summary": "Release a claimed feature",
        "responses": _lifecycle_responses(
            _changed_body_example(status="available"), conflict_message="invalid transition"
        ),
    },
    ("POST", "/api/projects/{project}/features/{feature}/ship"): {
        "summary": "Ship a feature",
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"outcome": {"type": ["string", "null"]}},
                    }
                }
            },
        },
        "responses": _lifecycle_responses(
            _changed_body_example(status="shipped"),
            conflict_message="invalid transition",
            include_400=True,
        ),
    },
    ("POST", "/api/projects/{project}/features/{feature}/archive"): {
        "summary": "Archive a feature",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "enum": list(ARCHIVE_REASONS),
                                "description": (
                                    "'subsumed', 'superseded', and 'duplicate' require "
                                    "'superseded_by'; 'obsolete' may stand alone."
                                ),
                            },
                            "superseded_by": {"type": ["string", "null"]},
                            "note": {"type": ["string", "null"]},
                            "actor": {"type": "string", "description": "Defaults to 'agent'."},
                        },
                        "required": ["reason"],
                    }
                }
            },
        },
        "responses": _lifecycle_responses(
            _changed_body_example(status="archived"),
            conflict_message="invalid transition",
            include_400=True,
        ),
    },
    ("POST", "/api/projects/{project}/features/{feature}/unarchive"): {
        "summary": "Unarchive a feature, returning it to available",
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "actor": {"type": "string", "description": "Defaults to 'agent'."}
                        },
                    }
                }
            },
        },
        "responses": _lifecycle_responses(
            _changed_body_example(status="available"), conflict_message="invalid transition"
        ),
    },
    ("POST", "/api/projects/{project}/features/{feature}/note"): {
        "summary": "Add a note to a feature",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"notes": {"type": "string"}},
                        "required": ["notes"],
                    }
                }
            },
        },
        "responses": {
            "200": _json_response("Note recorded", _changed_body_example(notes="…")),
            "400": _INVALID_JSON,
            "404": _error("Feature not found", "feature not found"),
            "503": _DB_NOT_CONFIGURED,
        },
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


def _merge_parameters(
    derived: list[dict[str, Any]], curated: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Overlay curated parameters onto derived path parameters.

    A curated entry naming an existing path parameter extends it (e.g. adds a
    description); any other curated entry (typically a query parameter) is
    appended as a new parameter.
    """
    merged = [dict(p) for p in derived]
    by_name = {p["name"]: p for p in merged}
    for extra in curated:
        existing = by_name.get(extra["name"])
        if existing is not None and existing.get("in") == extra.get("in", "path"):
            existing.update({k: v for k, v in extra.items() if k != "name"})
        else:
            merged.append(dict(extra))
    return merged


def build_spec(routes: list[Any], *, base_url: str, version: str) -> dict[str, Any]:
    """Assemble the OpenAPI 3.1 document from the live route table + curated metadata."""
    paths: dict[str, dict[str, Any]] = {}
    for route in routes:
        if not isinstance(route, Route) or not route.path.startswith("/api"):
            continue
        methods = {m for m in (route.methods or []) if m not in ("HEAD", "OPTIONS")}
        derived_parameters = [
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
            parameters = _merge_parameters(derived_parameters, meta.get("parameters", []))
            operation: dict[str, Any] = {"summary": meta.get("summary", "")}
            if parameters:
                operation["parameters"] = parameters
            if "requestBody" in meta:
                operation["requestBody"] = meta["requestBody"]
            if "responses" in meta:
                operation["responses"] = meta["responses"]
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
        "components": COMPONENTS,
    }


async def openapi_json(request: Request) -> JSONResponse:
    spec = build_spec(
        request.app.routes,
        base_url=public_base_url(),
        version=_api_version(),
    )
    return JSONResponse(spec)
