import dataclasses

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.templating import Jinja2Templates


async def index(request: Request) -> HTMLResponse:
    app = request.app
    templates: Jinja2Templates = app.state.templates
    if app.state.db_path is None:
        return templates.TemplateResponse(request, "index.html", {"configured": False})
    from feature_skills_webapp.storage.inbox import build_inbox
    from feature_skills_webapp.web.db_dep import request_conn

    project = request.query_params.get("project")
    with request_conn(app) as conn:
        inbox = build_inbox(conn, project=project)
        projects = [
            r["name"] for r in conn.execute("SELECT name FROM projects ORDER BY name").fetchall()
        ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "configured": True,
            "inbox": inbox,
            "projects": projects,
            "active_project": project,
        },
        headers={"Cache-Control": "no-store"},
    )


async def healthz(request: Request) -> JSONResponse:
    try:
        from feature_skills_webapp.web.db_dep import request_conn

        with request_conn(request.app) as conn:
            conn.execute("SELECT 1")
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


async def admin_mark_read(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    from feature_skills_webapp.storage.read_state import mark_all_read
    from feature_skills_webapp.web.db_dep import request_conn

    with request_conn(request.app) as conn:
        row = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()
        if row is None:
            return JSONResponse({"error": "unknown project"}, status_code=404)
        stamped = mark_all_read(conn, row["id"])
    return JSONResponse({"project": project, "stamped": stamped})


async def admin_discover(request: Request) -> JSONResponse:
    if not hasattr(request.app.state, "walk_queue"):
        return JSONResponse({"error": "discovery not configured"}, status_code=503)

    from feature_skills_webapp.web.discovery import request_walk

    summary = await request_walk(request.app, reconcile=True, await_result=True)
    if summary is None:
        return JSONResponse({"error": "no summary returned"}, status_code=500)

    return JSONResponse(dataclasses.asdict(summary))
