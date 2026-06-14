from __future__ import annotations

import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from feature_skills_webapp.storage.retro_findings import list_findings
from feature_skills_webapp.web.db_dep import request_conn


async def project_page(request: Request) -> Response:
    app = request.app
    if app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    name = request.path_params["project"]
    with request_conn(app) as conn:
        proj = conn.execute("SELECT id, name FROM projects WHERE name = ?", (name,)).fetchone()
        if proj is None:
            return PlainTextResponse("Not found", status_code=404)
        feats = conn.execute(
            "SELECT f.slug, f.status, f.owner, "
            "  (SELECT MAX(e.created_at) FROM events e "
            "   JOIN documents d ON e.document_id = d.id "
            "   WHERE d.feature_id = f.id AND d.status = 'active') AS last_activity "
            "FROM features f WHERE f.project_id = ? ORDER BY f.status, f.slug",
            (proj["id"],),
        ).fetchall()
        tracker = conn.execute(
            "SELECT id FROM documents "
            "WHERE project_id = ? AND feature_id IS NULL AND status = 'active'",
            (proj["id"],),
        ).fetchone()
        findings = list_findings(conn, proj["id"])

    in_progress = [f for f in feats if f["status"] == "in_progress"]
    available = [f for f in feats if f["status"] == "available"]
    done = [f for f in feats if f["status"] == "done"]

    def _feat(f: sqlite3.Row) -> dict[str, object]:
        return {
            "slug": f["slug"],
            "owner": f["owner"],
            "last_activity": f["last_activity"],
        }

    return app.state.templates.TemplateResponse(
        request,
        "project.html",
        {
            "project": proj["name"],
            "in_progress": [_feat(f) for f in in_progress],
            "available": [_feat(f) for f in available],
            "done": [_feat(f) for f in done],
            "tracker_id": tracker["id"] if tracker else None,
            "findings": findings,
        },
    )
