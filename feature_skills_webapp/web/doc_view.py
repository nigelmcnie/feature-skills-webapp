from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from markupsafe import Markup
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from feature_skills_webapp.storage.doc_content import manifest_for
from feature_skills_webapp.storage.doc_render import (
    FeedbackItem,
    extract_safe_inner,
    parse_feedback_items,
    render_section_doc,
)
from feature_skills_webapp.storage.inbox import doc_type_rank, humanise_type
from feature_skills_webapp.storage.read_state import mark_read
from feature_skills_webapp.storage.versions import current_content
from feature_skills_webapp.storage.walker import FEEDBACK_SUFFIX
from feature_skills_webapp.web.db_dep import request_conn

_STATIC_DIR = Path(__file__).parent / "static"


def _static_v(filename: str) -> str:
    """Return the mtime of a static file as a cache-buster version string."""
    try:
        return str(int((_STATIC_DIR / filename).stat().st_mtime))
    except OSError:
        return "0"


ROW_SQL = (
    "SELECT d.id, d.type, d.status, d.source_path, d.content_html, "
    "  p.name AS project, f.slug AS feature, f.id AS feature_id "
    "FROM documents d "
    "JOIN projects p ON d.project_id = p.id "
    "LEFT JOIN features f ON d.feature_id = f.id "
    "WHERE d.id = ?"
)


def breadcrumbs(row: sqlite3.Row) -> list[tuple[str, str | None]]:
    project_href = f"/project/{quote(row['project'], safe='')}"
    crumbs: list[tuple[str, str | None]] = [("Home", "/"), (row["project"], project_href)]
    if row["feature"] is None:  # project-level tracker doc
        crumbs.append((humanise_type("features"), None))
        return crumbs
    feature_href = f"{project_href}/feature/{quote(row['feature'], safe='')}"
    crumbs.append((row["feature"], feature_href))
    label = humanise_type(row["type"])
    if row["status"] == "archived":
        label += " (archived)"
    crumbs.append((label, None))
    return crumbs


def siblings(
    conn: sqlite3.Connection, feature_id: int, current_id: int
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    rows = conn.execute(
        "SELECT id, type FROM documents WHERE feature_id = ? AND status = 'active' AND type NOT LIKE ?",
        (feature_id, f"%{FEEDBACK_SUFFIX}"),
    ).fetchall()
    ordered = sorted(rows, key=lambda r: (doc_type_rank(r["type"]), r["id"]))
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

        # Determine render mode and build body_html
        body_html: Markup = Markup("")
        mode: str
        feedback_items: list[FeedbackItem] = []
        synthesis_responses: dict[int, str] = {}
        synthesis_flags: dict[int, str] = {}
        if not available:
            mode = "unavailable"
        elif is_synthesis:
            content = current_content(conn, doc_id)
            if content is not None and content.shape == "opaque":
                feedback_items = parse_feedback_items(content.sections[0].body)
                synth_rows = conn.execute(
                    "SELECT item_num, response, routine_flag FROM synthesis_responses "
                    "WHERE document_id = ?",
                    (doc_id,),
                ).fetchall()
                for r in synth_rows:
                    if r["routine_flag"] is not None:
                        synthesis_flags[r["item_num"]] = r["routine_flag"]
                    else:
                        synthesis_responses[r["item_num"]] = r["response"] or ""
                mode = "synthesis-native"
            else:
                mode = "raw-fallback"
        else:
            content = current_content(conn, doc_id)
            if content is None:
                mode = "raw-fallback"
            elif content.shape == "opaque":
                body_html = extract_safe_inner(content.sections[0].body)
                mode = "native"
            else:
                manifest = manifest_for(row["type"])
                body_html = render_section_doc(content, manifest)
                mode = "native"

        comments_prefill: list[dict[str, object]] = []
        if mode == "native" and is_commentable:
            rows = conn.execute(
                "SELECT id, excerpt, text FROM comments "
                "WHERE document_id = ? AND status = 'active' ORDER BY id",
                (doc_id,),
            ).fetchall()
            comments_prefill = [
                {"id": r["id"], "excerpt": r["excerpt"] or "", "text": r["text"]} for r in rows
            ]

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
            "mode": mode,
            "body_html": body_html,
            "feedback_items": feedback_items,
            "synthesis_responses": synthesis_responses,
            "synthesis_flags": synthesis_flags,
            "is_synthesis": is_synthesis,
            "synthesis_post_url": f"/doc/{doc_id}/synthesis-response",
            "is_commentable": is_commentable,
            "comment_post_url": f"/doc/{doc_id}/comments",
            "comments_prefill_json": Markup(json.dumps(comments_prefill)),
            "css_v": _static_v("doc.css"),
            "js_v": _static_v("doc.js"),
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
