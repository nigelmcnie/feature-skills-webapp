"""HTTP handlers for tracker listing and mutation endpoints."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from feature_skills_webapp.storage.db import now_iso, transaction
from feature_skills_webapp.storage.tracker import (
    FeatureExists,
    FeatureNotFound,
    InvalidTransition,
    capture_feature,
    claim_feature,
    get_feature,
    get_project,
    list_feature_documents,
    list_features,
    list_projects,
    park_feature,
    release_feature,
    ship_feature,
)
from feature_skills_webapp.web.db_dep import request_conn


async def list_projects_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    with request_conn(request.app) as conn:
        rows = list_projects(conn)
    return JSONResponse({"projects": [{"name": r["name"]} for r in rows]})


async def list_features_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    name = request.path_params["project"]
    with request_conn(request.app) as conn:
        proj = get_project(conn, name)
        if proj is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        feats = list_features(conn, proj["id"])
    return JSONResponse(
        {
            "project": name,
            "features": [
                {"slug": r["slug"], "status": r["status"], "owner": r["owner"], "notes": r["notes"]}
                for r in feats
            ],
        }
    )


async def list_documents_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]
    with request_conn(request.app) as conn:
        feat = get_feature(conn, project, slug)
        if feat is None:
            return JSONResponse({"error": "feature not found"}, status_code=404)
        docs = list_feature_documents(conn, feat["id"])
    return JSONResponse(
        {
            "project": project,
            "feature": slug,
            "documents": [
                {
                    "doc_type": r["type"],
                    "instance": r["instance"],
                    "logical_key": r["logical_key"],
                    "version": r["version"],
                    "document_id": r["id"],
                    "url": f"/doc/{r['id']}",
                }
                for r in docs
            ],
        }
    )


async def capture_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    notes_raw = body.get("notes")
    if notes_raw is not None and not isinstance(notes_raw, str):
        return JSONResponse({"error": "'notes' must be a string"}, status_code=400)
    notes: str | None = notes_raw

    try:
        with request_conn(request.app) as conn, transaction(conn):
            result = capture_feature(conn, project=project, slug=slug, notes=notes, now=now_iso())
    except FeatureExists:
        return JSONResponse({"error": "feature already exists"}, status_code=409)

    if result.changed:
        request.app.state.broadcaster.broadcast()
    return JSONResponse(
        {
            "project": result.project,
            "slug": result.slug,
            "status": result.status,
            "changed": result.changed,
        }
    )


async def claim_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return JSONResponse({"error": "'owner' must be a non-empty string"}, status_code=400)

    try:
        with request_conn(request.app) as conn, transaction(conn):
            result = claim_feature(conn, project=project, slug=slug, owner=owner, now=now_iso())
    except FeatureNotFound:
        return JSONResponse({"error": "feature not found"}, status_code=404)
    except InvalidTransition as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    if result.changed:
        request.app.state.broadcaster.broadcast()
    return JSONResponse(
        {
            "project": result.project,
            "slug": result.slug,
            "status": result.status,
            "changed": result.changed,
        }
    )


async def park_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]

    try:
        with request_conn(request.app) as conn, transaction(conn):
            result = park_feature(conn, project=project, slug=slug, now=now_iso())
    except FeatureNotFound:
        return JSONResponse({"error": "feature not found"}, status_code=404)
    except InvalidTransition as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    if result.changed:
        request.app.state.broadcaster.broadcast()
    return JSONResponse(
        {
            "project": result.project,
            "slug": result.slug,
            "status": result.status,
            "changed": result.changed,
        }
    )


async def release_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]

    try:
        with request_conn(request.app) as conn, transaction(conn):
            result = release_feature(conn, project=project, slug=slug, now=now_iso())
    except FeatureNotFound:
        return JSONResponse({"error": "feature not found"}, status_code=404)
    except InvalidTransition as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    if result.changed:
        request.app.state.broadcaster.broadcast()
    return JSONResponse(
        {
            "project": result.project,
            "slug": result.slug,
            "status": result.status,
            "changed": result.changed,
        }
    )


async def ship_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    outcome_raw = body.get("outcome")
    if outcome_raw is not None and not isinstance(outcome_raw, str):
        return JSONResponse({"error": "'outcome' must be a string"}, status_code=400)
    outcome: str | None = outcome_raw

    try:
        with request_conn(request.app) as conn, transaction(conn):
            result = ship_feature(conn, project=project, slug=slug, outcome=outcome, now=now_iso())
    except FeatureNotFound:
        return JSONResponse({"error": "feature not found"}, status_code=404)
    except InvalidTransition as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    if result.changed:
        request.app.state.broadcaster.broadcast()
    return JSONResponse(
        {
            "project": result.project,
            "slug": result.slug,
            "status": result.status,
            "changed": result.changed,
        }
    )
