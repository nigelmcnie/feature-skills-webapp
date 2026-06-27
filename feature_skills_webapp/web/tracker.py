"""HTTP handlers for tracker listing and mutation endpoints."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse

from feature_skills_webapp.storage.db import now_iso, transaction
from feature_skills_webapp.storage.tracker import (
    FeatureExists,
    FeatureNotFound,
    InvalidTransition,
    ProjectExists,
    ProjectNotFound,
    claim_feature,
    create_feature,
    create_project,
    drop_feature,
    get_feature,
    get_project,
    get_project_row,
    list_feature_documents,
    list_features,
    list_projects,
    park_feature,
    release_feature,
    set_project_suggested_order,
    ship_feature,
    update_feature_note,
)
from feature_skills_webapp.web.db_dep import request_conn
from feature_skills_webapp.web.submit import _NOTICES, missing_project_msg


async def create_project_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    name = request.path_params["project"]
    try:
        with request_conn(request.app) as conn, transaction(conn):
            create_project(conn, name=name, now=now_iso())
    except ProjectExists:
        return JSONResponse({"error": "project already exists"}, status_code=409)
    request.app.state.broadcaster.broadcast()
    return JSONResponse({"project": name})


async def get_project_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    name = request.path_params["project"]
    with request_conn(request.app) as conn:
        row = get_project_row(conn, name)
    if row is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse(
        {
            "project": row["name"],
            "repo_path": row["repo_path"],
            "suggested_order": row["suggested_order"],
        }
    )


async def put_suggested_order_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    name = request.path_params["project"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    text = body.get("text")
    if text is not None and not isinstance(text, str):
        return JSONResponse({"error": "'text' must be a string"}, status_code=400)
    with request_conn(request.app) as conn:
        row = get_project_row(conn, name)
        if row is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        with transaction(conn):
            set_project_suggested_order(conn, name, text or None)
        updated = get_project_row(conn, name)
        assert updated is not None
    return JSONResponse(
        {
            "project": updated["name"],
            "suggested_order": updated["suggested_order"],
        }
    )


async def list_projects_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    with request_conn(request.app) as conn:
        rows = list_projects(conn)
    return JSONResponse({"projects": [{"name": r["name"]} for r in rows], "notices": _NOTICES})


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
                {
                    "slug": r["slug"],
                    "status": r["status"],
                    "owner": r["owner"],
                    "notes": r["notes"],
                    "created_at": r["created_at"],
                }
                for r in feats
            ],
            "notices": _NOTICES,
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


async def create_feature_handler(request: Request) -> JSONResponse:
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
            result = create_feature(conn, project=project, slug=slug, notes=notes, now=now_iso())
    except FeatureExists:
        return JSONResponse({"error": "feature already exists"}, status_code=409)
    except ProjectNotFound:
        return JSONResponse({"error": missing_project_msg(project)}, status_code=404)

    request.app.state.broadcaster.broadcast()
    return JSONResponse(
        {
            "project": result.project,
            "slug": result.slug,
            "status": result.status,
            "changed": result.changed,
        }
    )


async def get_feature_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]
    with request_conn(request.app) as conn:
        proj = get_project(conn, project)
        if proj is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        feat = get_feature(conn, project, slug)
    if feat is None:
        return JSONResponse({"error": "feature not found"}, status_code=404)
    return JSONResponse(
        {
            "project": feat["project"],
            "slug": feat["slug"],
            "status": feat["status"],
            "owner": feat["owner"],
            "notes": feat["notes"],
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


async def note_handler(request: Request) -> JSONResponse:
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
    notes = body.get("notes")
    if not isinstance(notes, str):
        return JSONResponse({"error": "'notes' must be a string"}, status_code=400)
    try:
        with request_conn(request.app) as conn, transaction(conn):
            result = update_feature_note(
                conn, project=project, slug=slug, notes=notes, now=now_iso()
            )
    except FeatureNotFound:
        return JSONResponse({"error": "feature not found"}, status_code=404)
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


async def drop_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    try:
        with request_conn(request.app) as conn, transaction(conn):
            result = drop_feature(conn, project=project, slug=slug, now=now_iso())
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
