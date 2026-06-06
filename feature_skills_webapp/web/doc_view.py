from __future__ import annotations

import sqlite3
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from feature_skills_webapp.storage.inbox import humanise_type
from feature_skills_webapp.storage.read_state import mark_read
from feature_skills_webapp.web.db_dep import request_conn

ROW_SQL = (
    "SELECT d.id, d.type, d.status, d.source_path, d.content_html, "
    "  p.name AS project, f.slug AS feature "
    "FROM documents d "
    "JOIN projects p ON d.project_id = p.id "
    "LEFT JOIN features f ON d.feature_id = f.id "
    "WHERE d.id = ?"
)


def breadcrumbs(row: sqlite3.Row) -> list[tuple[str, str | None]]:
    crumbs: list[tuple[str, str | None]] = [(row["project"], None)]
    if row["feature"] is None:  # project-level tracker doc
        crumbs.append(("Tracker", None))
        return crumbs
    crumbs.append((row["feature"], None))
    label = humanise_type(row["type"])
    if row["status"] == "archived":
        label += " (archived)"
    crumbs.append((label, None))
    return crumbs


async def doc_shell(request: Request) -> Response:
    app = request.app
    if app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    doc_id = request.path_params["document_id"]
    with request_conn(app) as conn:
        row = conn.execute(ROW_SQL, (doc_id,)).fetchone()
        if row is None:
            return PlainTextResponse("Not found", status_code=404)
        crumbs = breadcrumbs(row)
        available = row["status"] in ("active", "archived")
        mark_read(conn, doc_id)  # own transaction; after the read
    return app.state.templates.TemplateResponse(
        request,
        "doc.html",
        {
            "doc_id": doc_id,
            "crumbs": crumbs,
            "available": available,
            "raw_url": f"/doc/{doc_id}/raw",
            "siblings": None,
        },
    )


async def doc_raw(request: Request) -> Response:
    app = request.app
    if app.state.db_path is None:
        return PlainTextResponse("Not found", status_code=404)
    doc_id = request.path_params["document_id"]
    with request_conn(app) as conn:
        row = conn.execute(
            "SELECT status, source_path, content_html FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
    if row is None or row["status"] not in ("active", "archived"):
        return PlainTextResponse("Not found", status_code=404)
    if row["content_html"]:
        return HTMLResponse(row["content_html"])
    if not row["source_path"]:
        return PlainTextResponse("Not found", status_code=404)
    try:
        html = Path(row["source_path"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return PlainTextResponse("Not found", status_code=404)
    return HTMLResponse(html)
