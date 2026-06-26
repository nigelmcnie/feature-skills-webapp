"""HTTP handlers for agent-submission endpoints (write + read by logical identity)."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse

from feature_skills_webapp.storage.db import now_iso, transaction
from feature_skills_webapp.storage.doc_content import manifest_for
from feature_skills_webapp.storage.documents import (
    SubmitError,
    build_content,
    submit_document,
    validate_writable,
)
from feature_skills_webapp.storage.versions import current_content
from feature_skills_webapp.storage.walker import logical_key
from feature_skills_webapp.web.db_dep import request_conn


def _path_params(request: Request) -> tuple[str, str | None, str, int]:
    """Extract and normalise logical-identity path params from a request.

    Returns (project, feat, doc_type, instance) where feat is None when the
    URL segment is '-' (project-level sentinel).
    """
    feature_param: str = request.path_params["feature"]
    return (
        request.path_params["project"],
        None if feature_param == "-" else feature_param,
        request.path_params["doc_type"],
        request.path_params["instance"],
    )


# ---------------------------------------------------------------------------
# Phase 1: write path
# ---------------------------------------------------------------------------


async def put_document(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    project, feat, doc_type, instance = _path_params(request)
    dry_run = request.query_params.get("dry_run", "").lower() in ("1", "true")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    actor_raw = body.get("actor")
    if actor_raw is not None and not isinstance(actor_raw, str):
        return JSONResponse({"error": "'actor' must be a string"}, status_code=400)
    actor: str = actor_raw or "agent"

    extra_css_raw = body.get("extra_css")
    if extra_css_raw is not None and not isinstance(extra_css_raw, str):
        return JSONResponse({"error": "'extra_css' must be a string"}, status_code=400)

    try:
        validate_writable(doc_type, feat, instance)
        content = build_content(doc_type, body.get("sections"), body.get("body"), extra_css_raw)
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
            "url": (
                f"/doc/{result.document_id}"
                if result.created
                else f"/doc/{result.document_id}?view=diff"
            ),
            "created": result.created,
            "changed": result.changed,
        }
    )


# ---------------------------------------------------------------------------
# Phase 2: read round-trips
# ---------------------------------------------------------------------------


async def get_document(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    project, feat, doc_type, instance = _path_params(request)
    lkey = logical_key(project, feat, doc_type, instance)

    with request_conn(request.app) as conn:
        row = conn.execute("SELECT id FROM documents WHERE logical_key=?", (lkey,)).fetchone()
        if row is None:
            return JSONResponse({"error": "document not found"}, status_code=404)
        doc_id = row["id"]
        cur = current_content(conn, doc_id)
        ver_row = conn.execute(
            "SELECT COALESCE(MAX(version_num), 0) AS ver "
            "FROM document_versions WHERE document_id=?",
            (doc_id,),
        ).fetchone()

    shape = manifest_for(doc_type).shape if cur is None else cur.shape
    sections = [] if cur is None else [{"key": s.key, "body": s.body} for s in cur.sections]
    extra_css = "" if cur is None else cur.extra_css
    return JSONResponse(
        {
            "logical_key": lkey,
            "document_id": doc_id,
            "doc_type": doc_type,
            "shape": shape,
            "sections": sections,
            "extra_css": extra_css,
            "version_num": ver_row["ver"],
            "url": (f"/doc/{doc_id}?view=diff" if ver_row["ver"] > 1 else f"/doc/{doc_id}"),
        }
    )


_PRESENTATION = {
    "stylesheet_url": "/static/doc.css",
    "extra_css": (
        "Optional top-level field on document writes. Scoped to the document body and"
        " flagged for review. Base stylesheet rules still apply per-property; extra_css"
        " layers on top — to adjust an existing rule, match its specificity (or add new"
        " properties). Use only when the stylesheet vocabulary doesn't cover what you need."
    ),
}


async def get_manifest(request: Request) -> JSONResponse:
    """Return the section manifest for a doc type. No DB required."""
    doc_type: str = request.path_params["doc_type"]
    spec = manifest_for(doc_type)
    return JSONResponse(
        {
            "doc_type": doc_type,
            "shape": spec.shape,
            "sections": [{"key": k, "label": lbl} for k, lbl in spec.section_labels],
            "repeated_prefixes": list(spec.repeated_prefixes),
            "presentation": _PRESENTATION,
        }
    )


async def get_document_comments(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    project, feat, doc_type, instance = _path_params(request)
    lkey = logical_key(project, feat, doc_type, instance)

    with request_conn(request.app) as conn:
        row = conn.execute("SELECT id FROM documents WHERE logical_key=?", (lkey,)).fetchone()
        if row is None:
            return JSONResponse({"error": "document not found"}, status_code=404)
        doc_id = row["id"]

        any_row = conn.execute(
            "SELECT 1 FROM comments WHERE document_id=? LIMIT 1", (doc_id,)
        ).fetchone()
        active_rows = conn.execute(
            "SELECT id, excerpt, text FROM comments "
            "WHERE document_id=? AND status='active' ORDER BY id",
            (doc_id,),
        ).fetchall()

    return JSONResponse(
        {
            "doc": lkey,
            "submitted": bool(any_row),
            "comments": [
                {"id": r["id"], "excerpt": r["excerpt"], "text": r["text"]} for r in active_rows
            ],
        }
    )


async def post_document_comments_integrate(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    project, feat, doc_type, instance = _path_params(request)
    lkey = logical_key(project, feat, doc_type, instance)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    ids = body.get("ids")
    if not isinstance(ids, list):
        return JSONResponse({"error": "'ids' must be a list"}, status_code=400)
    for item in ids:
        if not isinstance(item, int):
            return JSONResponse({"error": "'ids' must be a list of integers"}, status_code=400)

    with request_conn(request.app) as conn:
        row = conn.execute("SELECT id FROM documents WHERE logical_key=?", (lkey,)).fetchone()
        if row is None:
            return JSONResponse({"error": "document not found"}, status_code=404)
        doc_id = row["id"]
        now = now_iso()
        count = 0
        with transaction(conn):
            for cid in ids:
                conn.execute(
                    "UPDATE comments SET status='integrated', integrated_at=? "
                    "WHERE id=? AND document_id=? AND status='active'",
                    (now, cid, doc_id),
                )
                count += conn.execute("SELECT changes()").fetchone()[0]
            conn.execute(
                "INSERT INTO events (document_id, event_type, payload_json, created_at) "
                "VALUES (?, 'comment_integrated', ?, ?)",
                (doc_id, json.dumps({"count": count}), now),
            )

    return JSONResponse({"integrated": count})


async def get_document_synthesis(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    project, feat, doc_type, instance = _path_params(request)
    lkey = logical_key(project, feat, doc_type, instance)

    with request_conn(request.app) as conn:
        row = conn.execute("SELECT id FROM documents WHERE logical_key=?", (lkey,)).fetchone()
        if row is None:
            return JSONResponse({"error": "document not found"}, status_code=404)
        doc_id = row["id"]

        rows = conn.execute(
            "SELECT item_num, response, routine_flag FROM synthesis_responses WHERE document_id=?",
            (doc_id,),
        ).fetchall()

    responses = {str(r["item_num"]): r["response"] for r in rows if r["routine_flag"] is None}
    routine_flags = {
        str(r["item_num"]): r["routine_flag"] for r in rows if r["routine_flag"] is not None
    }
    return JSONResponse(
        {
            "doc": lkey,
            "submitted": bool(rows),
            "responses": responses,
            "routine_flags": routine_flags,
        }
    )
