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
    archived = [f for f in feats if f["status"] == "archived"]
    # Newest first; legacy rows with no archived_at (dropped before this
    # metadata existed) sort last since they have nothing to order by.
    archived_sorted = sorted(
        archived,
        key=lambda f: (f["archived_at"] is not None, f["archived_at"] or ""),
        reverse=True,
    )

    def _feat(f: sqlite3.Row) -> dict[str, object]:
        return {
            "slug": f["slug"],
            "owner": f["owner"],
            "last_activity": f["last_activity"],
        }

    feats_by_slug = {f["slug"]: f for f in feats}

    def _archived_feat(f: sqlite3.Row) -> dict[str, object]:
        sb = f["superseded_by"]
        return {
            "slug": f["slug"],
            "owner": f["owner"],
            "reason": f["archive_reason"],
            "note": f["archive_note"],
            "superseded_by": sb,
            "superseded_by_slug": sb if (sb and sb in feats_by_slug) else None,
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
            "archived": [_archived_feat(f) for f in archived_sorted],
            "tracker_id": tracker["id"] if tracker else None,
            "findings": findings,
        },
    )
