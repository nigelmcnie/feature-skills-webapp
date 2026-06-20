"""HTTP handlers for tracker listing endpoints (read-only)."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from feature_skills_webapp.storage.tracker import (
    get_feature,
    get_project,
    list_feature_documents,
    list_features,
    list_projects,
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
