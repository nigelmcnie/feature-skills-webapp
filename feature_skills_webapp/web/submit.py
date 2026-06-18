"""HTTP handlers for agent-submission endpoints (Phase 1: write path)."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from feature_skills_webapp.storage.db import now_iso, transaction
from feature_skills_webapp.storage.documents import (
    SubmitError,
    build_content,
    submit_document,
    validate_writable,
)
from feature_skills_webapp.web.db_dep import request_conn


async def put_document(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    project: str = request.path_params["project"]
    feature_param: str = request.path_params["feature"]
    doc_type: str = request.path_params["doc_type"]
    instance: int = request.path_params["instance"]

    feat = None if feature_param == "-" else feature_param
    dry_run = request.query_params.get("dry_run", "").lower() in ("1", "true")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    actor: str = body.get("actor") or "agent"

    try:
        validate_writable(doc_type, feat, instance)
        content = build_content(doc_type, body.get("sections"), body.get("body"))
    except SubmitError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if dry_run:
        return JSONResponse({"valid": True})

    with request_conn(request.app) as conn, transaction(conn):
        result = submit_document(
            conn,
            project=project,
            feature=feat,
            doc_type=doc_type,
            instance=instance,
            content=content,
            actor=actor,
            now=now_iso(),
        )

    request.app.state.broadcaster.broadcast()

    return JSONResponse(
        {
            "logical_key": result.logical_key,
            "document_id": result.document_id,
            "version_num": result.version_num,
            "url": f"/doc/{result.document_id}",
            "created": result.created,
            "changed": result.changed,
        }
    )
