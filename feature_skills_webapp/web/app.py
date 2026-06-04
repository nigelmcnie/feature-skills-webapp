import contextlib
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from feature_skills_webapp.web.routes import healthz, index

_HERE = Path(__file__).parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


def create_app(db_path: Path | None) -> Starlette:
    jinja_env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    templates = Jinja2Templates(env=jinja_env)

    @contextlib.asynccontextmanager
    async def lifespan(app):  # type: ignore[no-untyped-def]
        if db_path is not None:
            from feature_skills_webapp.storage.db import connect, migrate

            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = connect(db_path)
            migrate(conn)
            conn.close()
        yield

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/healthz", healthz),
            Mount("/static", StaticFiles(directory=STATIC_DIR)),
        ],
        lifespan=lifespan,
    )
    app.state.templates = templates
    app.state.db_path = db_path
    return app
