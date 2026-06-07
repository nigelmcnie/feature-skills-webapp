from __future__ import annotations

import sqlite3
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from feature_skills_webapp.storage.inbox import DOC_TYPE_ORDER, humanise_type
from feature_skills_webapp.storage.read_state import mark_read
from feature_skills_webapp.storage.walker import FEEDBACK_SUFFIX
from feature_skills_webapp.web.db_dep import request_conn

ROW_SQL = (
    "SELECT d.id, d.type, d.status, d.source_path, d.content_html, "
    "  p.name AS project, f.slug AS feature, f.id AS feature_id "
    "FROM documents d "
    "JOIN projects p ON d.project_id = p.id "
    "LEFT JOIN features f ON d.feature_id = f.id "
    "WHERE d.id = ?"
)


def breadcrumbs(row: sqlite3.Row) -> list[tuple[str, str | None]]:
    crumbs: list[tuple[str, str | None]] = [(row["project"], None)]
    if row["feature"] is None:  # project-level tracker doc
        crumbs.append((humanise_type("features"), None))
        return crumbs
    crumbs.append((row["feature"], None))
    label = humanise_type(row["type"])
    if row["status"] == "archived":
        label += " (archived)"
    crumbs.append((label, None))
    return crumbs


def _rank(t: str) -> int:
    return DOC_TYPE_ORDER.index(t) if t in DOC_TYPE_ORDER else len(DOC_TYPE_ORDER)


def siblings(
    conn: sqlite3.Connection, feature_id: int, current_id: int
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    rows = conn.execute(
        "SELECT id, type FROM documents WHERE feature_id = ? AND status = 'active' AND type NOT LIKE ?",
        (feature_id, f"%{FEEDBACK_SUFFIX}"),
    ).fetchall()
    ordered = sorted(rows, key=lambda r: (_rank(r["type"]), r["id"]))
    ids = [r["id"] for r in ordered]
    if current_id not in ids:
        return None, None
    i = ids.index(current_id)
    prev = ordered[i - 1] if i > 0 else None
    nxt = ordered[i + 1] if i < len(ids) - 1 else None
    return prev, nxt


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
        is_synthesis = row["type"].endswith(FEEDBACK_SUFFIX) and row["status"] == "active"
        is_commentable = (
            row["type"] in {"requirements", "plan"}
            and row["status"] == "active"
            and row["feature"] is not None
        )
        nav: tuple[sqlite3.Row | None, sqlite3.Row | None] = (None, None)
        if row["feature"] is not None and row["status"] == "active":
            nav = siblings(conn, row["feature_id"], doc_id)
        mark_read(conn, doc_id)  # own transaction; after the read
    prev, nxt = nav
    return app.state.templates.TemplateResponse(
        request,
        "doc.html",
        {
            "doc_id": doc_id,
            "crumbs": crumbs,
            "available": available,
            "raw_url": f"/doc/{doc_id}/raw",
            "is_synthesis": is_synthesis,
            "synthesis_post_url": f"/doc/{doc_id}/synthesis-response",
            "is_commentable": is_commentable,
            "comment_post_url": f"/doc/{doc_id}/comments",
            "prev": {"id": prev["id"], "label": humanise_type(prev["type"])} if prev else None,
            "next": {"id": nxt["id"], "label": humanise_type(nxt["type"])} if nxt else None,
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
