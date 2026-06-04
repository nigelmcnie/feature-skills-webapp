from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from feature_skills_webapp.web.routes import index

_HERE = Path(__file__).parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


def create_app(db_path: Path | None) -> Starlette:
    jinja_env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    templates = Jinja2Templates(env=jinja_env)

    app = Starlette(
        routes=[
            Route("/", index),
            Mount("/static", StaticFiles(directory=STATIC_DIR)),
        ]
    )
    app.state.templates = templates
    app.state.db_path = db_path
    return app
