from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.templating import Jinja2Templates


async def index(request: Request) -> HTMLResponse:
    app = request.app
    templates: Jinja2Templates = app.state.templates
    fragment = request.query_params.get("fragment")
    template = "_inbox_body.html" if fragment else "index.html"
    headers = {"Cache-Control": "no-store"}
    if app.state.db_path is None:
        return templates.TemplateResponse(request, template, {"configured": False}, headers=headers)
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
        template,
        {
            "configured": True,
            "inbox": inbox,
            "projects": projects,
            "active_project": project,
        },
        headers=headers,
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


async def admin_mark_new_since_read(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.query_params.get("project")
    from feature_skills_webapp.storage.inbox import mark_new_since_read
    from feature_skills_webapp.web.db_dep import request_conn

    with request_conn(request.app) as conn:
        project_id = None
        if project is not None:
            row = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()
            if row is None:
                return JSONResponse({"error": "unknown project"}, status_code=404)
            project_id = row["id"]
        stamped = mark_new_since_read(conn, project_id)
    return JSONResponse({"stamped": stamped})
