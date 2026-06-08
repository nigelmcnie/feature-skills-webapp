"""Comment capture: write and read endpoint handlers."""

from __future__ import annotations

import json
from typing import cast

from starlette.requests import Request
from starlette.responses import JSONResponse

from feature_skills_webapp.storage.db import now_iso, transaction
from feature_skills_webapp.web.db_dep import request_conn

_MAX_VALUE_BYTES = 1024 * 1024  # 1 MB


async def post_comments(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    doc_id: int = request.path_params["document_id"]

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    comments = body.get("comments")
    if not isinstance(comments, list):
        return JSONResponse({"error": "'comments' must be a list"}, status_code=400)

    # Validate each item before opening a transaction.
    for i, c in enumerate(comments):
        if not isinstance(c, dict):
            return JSONResponse({"error": f"comment[{i}] must be an object"}, status_code=400)
        item = cast("dict[str, object]", c)
        text = item.get("text")
        if not isinstance(text, str):
            return JSONResponse({"error": f"comment[{i}].text must be a string"}, status_code=400)
        if len(text.encode()) > _MAX_VALUE_BYTES:
            return JSONResponse({"error": f"comment[{i}].text exceeds 1 MB"}, status_code=400)
        excerpt = item.get("excerpt")
        if excerpt is not None and not isinstance(excerpt, str):
            return JSONResponse(
                {"error": f"comment[{i}].excerpt must be a string or null"}, status_code=400
            )
        if isinstance(excerpt, str) and len(excerpt.encode()) > _MAX_VALUE_BYTES:
            return JSONResponse({"error": f"comment[{i}].excerpt exceeds 1 MB"}, status_code=400)

    with request_conn(request.app) as conn:
        if conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,)).fetchone() is None:
            return JSONResponse({"error": "document not found"}, status_code=404)

        now = now_iso()
        with transaction(conn):
            conn.execute(
                "DELETE FROM comments WHERE document_id = ? AND status = 'active'",
                (doc_id,),
            )
            for c in comments:
                conn.execute(
                    "INSERT INTO comments (document_id, excerpt, text, status, created_at) "
                    "VALUES (?, ?, ?, 'active', ?)",
                    (doc_id, c.get("excerpt"), c["text"], now),
                )
            conn.execute(
                "INSERT INTO events (document_id, event_type, payload_json, created_at) "
                "VALUES (?, 'comment_submitted', ?, ?)",
                (doc_id, json.dumps({"count": len(comments)}), now),
            )

    request.app.state.broadcaster.broadcast()
    return JSONResponse({"document_id": doc_id, "comments_written": len(comments)})


async def post_comments_integrate(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    path = body.get("path")
    if not isinstance(path, str):
        return JSONResponse({"error": "'path' must be a string"}, status_code=400)

    ids = body.get("ids")
    if not isinstance(ids, list):
        return JSONResponse({"error": "'ids' must be a list"}, status_code=400)
    for item in ids:
        if not isinstance(item, int):
            return JSONResponse({"error": "'ids' must be a list of integers"}, status_code=400)

    with request_conn(request.app) as conn:
        doc_row = conn.execute("SELECT id FROM documents WHERE source_path = ?", (path,)).fetchone()
        if doc_row is None:
            return JSONResponse({"error": "document not found"}, status_code=404)

        doc_id = doc_row["id"]
        now = now_iso()
        count = 0
        with transaction(conn):
            for cid in ids:
                conn.execute(
                    "UPDATE comments SET status = 'integrated', integrated_at = ? "
                    "WHERE id = ? AND document_id = ? AND status = 'active'",
                    (now, cid, doc_id),
                )
                count += conn.execute("SELECT changes()").fetchone()[0]
            conn.execute(
                "INSERT INTO events (document_id, event_type, payload_json, created_at) "
                "VALUES (?, 'comment_integrated', ?, ?)",
                (doc_id, json.dumps({"count": count}), now),
            )

    return JSONResponse({"integrated": count})


async def get_comments(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    path = request.query_params.get("path")
    if not path:
        return JSONResponse({"error": "path parameter required"}, status_code=400)

    with request_conn(request.app) as conn:
        doc_row = conn.execute("SELECT id FROM documents WHERE source_path = ?", (path,)).fetchone()
        if doc_row is None:
            return JSONResponse({"error": "document not found"}, status_code=404)

        any_row = conn.execute(
            "SELECT 1 FROM comments WHERE document_id = ? LIMIT 1",
            (doc_row["id"],),
        ).fetchone()

        active_rows = conn.execute(
            "SELECT id, excerpt, text FROM comments "
            "WHERE document_id = ? AND status = 'active' ORDER BY id",
            (doc_row["id"],),
        ).fetchall()

    return JSONResponse(
        {
            "doc": path,
            "submitted": bool(any_row),
            "comments": [
                {"id": r["id"], "excerpt": r["excerpt"], "text": r["text"]} for r in active_rows
            ],
        }
    )
