from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.templating import Jinja2Templates

MARKER = "feature-skills-webapp-placeholder"


async def index(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, "index.html", {"marker": MARKER})


async def healthz(request: Request) -> JSONResponse:
    try:
        from feature_skills_webapp.web.db_dep import request_conn

        with request_conn(request.app) as conn:
            conn.execute("SELECT 1")
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})
