from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

MARKER = "feature-skills-webapp-placeholder"


async def index(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, "index.html", {"marker": MARKER})
