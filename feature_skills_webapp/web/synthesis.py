"""Synthesis response capture: write and read endpoint handlers."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from feature_skills_webapp.storage.db import now_iso, transaction
from feature_skills_webapp.web.db_dep import request_conn

_MAX_VALUE_BYTES = 1024 * 1024  # 1 MB


async def post_synthesis_response(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    doc_id: int = request.path_params["document_id"]

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    responses = body.get("responses", {})
    routine_flags = body.get("routine_flags", {})

    if not isinstance(responses, dict):
        return JSONResponse({"error": "'responses' must be an object"}, status_code=400)
    if not isinstance(routine_flags, dict):
        return JSONResponse({"error": "'routine_flags' must be an object"}, status_code=400)

    # Validate all keys are integer strings and values are strings within 1 MB — before BEGIN IMMEDIATE.
    for key, val in {**responses, **routine_flags}.items():
        try:
            int(key)
        except ValueError, TypeError:
            return JSONResponse({"error": f"item key is not an integer: {key!r}"}, status_code=400)
        if not isinstance(val, str):
            return JSONResponse(
                {"error": f"item value must be a string: key {key!r}"}, status_code=400
            )
        if len(val.encode()) > _MAX_VALUE_BYTES:
            return JSONResponse({"error": f"item value exceeds 1 MB: key {key!r}"}, status_code=400)

    with request_conn(request.app) as conn:
        if conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,)).fetchone() is None:
            return JSONResponse({"error": "document not found"}, status_code=404)

        now = now_iso()
        with transaction(conn):
            conn.execute("DELETE FROM synthesis_responses WHERE document_id = ?", (doc_id,))
            for item, text in responses.items():
                conn.execute(
                    "INSERT INTO synthesis_responses "
                    "(document_id, item_num, response, routine_flag, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    (doc_id, int(item), text, now),
                )
            for item, comment in routine_flags.items():
                conn.execute(
                    "INSERT INTO synthesis_responses "
                    "(document_id, item_num, response, routine_flag, updated_at) "
                    "VALUES (?, ?, NULL, ?, ?)",
                    (doc_id, int(item), comment, now),
                )

    items_written = len(responses) + len(routine_flags)
    request.app.state.broadcaster.broadcast()
    return JSONResponse({"document_id": doc_id, "items_written": items_written})
