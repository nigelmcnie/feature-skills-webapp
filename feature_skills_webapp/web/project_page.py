from __future__ import annotations

import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from feature_skills_webapp.storage.retro_findings import list_findings
from feature_skills_webapp.storage.tracker import get_project, list_features
from feature_skills_webapp.web.db_dep import request_conn


async def project_page(request: Request) -> Response:
    app = request.app
    if app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    name = request.path_params["project"]
    with request_conn(app) as conn:
        proj = get_project(conn, name)
        if proj is None:
            return PlainTextResponse("Not found", status_code=404)
        feats = list_features(conn, proj["id"])
        tracker = conn.execute(
            "SELECT id FROM documents "
            "WHERE project_id = ? AND feature_id IS NULL AND status = 'active'",
            (proj["id"],),
        ).fetchone()
        findings = list_findings(conn, proj["id"])

    in_progress = [f for f in feats if f["status"] == "in_progress"]
    available = [f for f in feats if f["status"] == "available"]
    parked = [f for f in feats if f["status"] == "parked"]
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
            "parked": [_feat(f) for f in parked],
            "done": [_feat(f) for f in done],
            "tracker_id": tracker["id"] if tracker else None,
            "findings": findings,
        },
    )
