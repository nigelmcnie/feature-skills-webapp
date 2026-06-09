from __future__ import annotations

import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from feature_skills_webapp.storage.inbox import (
    DOC_TYPE_ORDER,
    badge_kind,
    doc_type_rank,
    humanise_type,
)
from feature_skills_webapp.storage.walker import FEEDBACK_SUFFIX
from feature_skills_webapp.web.db_dep import request_conn


async def feature_page(request: Request) -> Response:
    app = request.app
    if app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["slug"]
    with request_conn(app) as conn:
        feat = conn.execute(
            "SELECT f.id, f.slug, f.status, f.owner, f.notes, p.name AS project "
            "FROM features f JOIN projects p ON f.project_id = p.id "
            "WHERE p.name = ? AND f.slug = ?",
            (project, slug),
        ).fetchone()
        if feat is None:
            return PlainTextResponse("Not found", status_code=404)
        docs = conn.execute(
            "SELECT d.id, d.type, d.status, "
            "  (NOT EXISTS (SELECT 1 FROM synthesis_responses sr "
            "               WHERE sr.document_id = d.id)) AS awaiting "
            "FROM documents d WHERE d.feature_id = ?",
            (feat["id"],),
        ).fetchall()

    primary = sorted(
        [d for d in docs if d["status"] == "active" and d["type"] in DOC_TYPE_ORDER],
        key=lambda d: (doc_type_rank(d["type"]), d["id"]),
    )
    feedback = sorted(
        [d for d in docs if d["status"] == "active" and d["type"].endswith(FEEDBACK_SUFFIX)],
        key=lambda d: d["id"],
    )
    archived = sorted(
        [d for d in docs if d["status"] == "archived"],
        key=lambda d: (doc_type_rank(d["type"]), d["id"]),
    )

    def _ctx(d: sqlite3.Row) -> dict[str, object]:
        return {
            "id": d["id"],
            "label": humanise_type(d["type"]),
            "badge": badge_kind(d["type"]),
            "awaiting": bool(d["awaiting"]),
        }

    return app.state.templates.TemplateResponse(
        request,
        "feature.html",
        {
            "project": feat["project"],
            "slug": feat["slug"],
            "status": feat["status"],
            "owner": feat["owner"],
            "notes": feat["notes"],
            "primary": [_ctx(d) for d in primary],
            "feedback": [_ctx(d) for d in feedback],
            "archived": [_ctx(d) for d in archived],
        },
    )
