import asyncio
import contextlib
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from feature_skills_webapp.web.routes import admin_discover, healthz, index

_HERE = Path(__file__).parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

log = logging.getLogger(__name__)


def create_app(db_path: Path | None, docs_root: Path | None = None) -> Starlette:
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

        worker = watch = None
        if db_path is not None and docs_root is not None:
            from feature_skills_webapp.web.discovery import _watch, _worker, request_walk

            app.state.walk_queue = asyncio.Queue()
            worker = asyncio.create_task(_worker(app))
            watch = asyncio.create_task(_watch(app))
            await request_walk(app, reconcile=True, await_result=False)

        try:
            yield
        finally:
            for task in (watch, worker):
                if task:
                    task.cancel()
            await asyncio.gather(*[t for t in (watch, worker) if t], return_exceptions=True)

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/healthz", healthz),
            Route("/admin/discover", admin_discover, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC_DIR)),
        ],
        lifespan=lifespan,
    )
    app.state.templates = templates
    app.state.db_path = db_path
    app.state.docs_root = docs_root
    return app
