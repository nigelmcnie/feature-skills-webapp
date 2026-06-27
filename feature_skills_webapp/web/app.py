import contextlib
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from feature_skills_webapp.web.broadcaster import Broadcaster
from feature_skills_webapp.web.comments import post_comments
from feature_skills_webapp.web.doc_view import doc_shell
from feature_skills_webapp.web.events import events
from feature_skills_webapp.web.feature_page import feature_page
from feature_skills_webapp.web.project_page import project_page
from feature_skills_webapp.web.retro_findings import (
    get_retro_findings,
    post_retro_finding_status,
    post_retro_findings,
)
from feature_skills_webapp.web.routes import (
    admin_mark_new_since_read,
    admin_mark_read,
    healthz,
    index,
)
from feature_skills_webapp.web.submit import (
    get_document,
    get_document_comments,
    get_document_synthesis,
    get_manifest,
    post_document_comments_integrate,
    put_document,
)
from feature_skills_webapp.web.synthesis import post_synthesis_response
from feature_skills_webapp.web.tracker import (
    capture_handler,
    claim_handler,
    create_feature_handler,
    drop_handler,
    get_feature_handler,
    list_documents_handler,
    list_features_handler,
    list_projects_handler,
    note_handler,
    park_handler,
    release_handler,
    ship_handler,
)

_HERE = Path(__file__).parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

log = logging.getLogger(__name__)


def create_app(db_path: Path | None) -> Starlette:
    jinja_env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    templates = Jinja2Templates(env=jinja_env)

    @contextlib.asynccontextmanager
    async def lifespan(app):  # type: ignore[no-untyped-def]
        app.state.broadcaster = Broadcaster()

        if db_path is not None:
            from feature_skills_webapp.storage.db import connect, migrate
            from feature_skills_webapp.storage.versions import backfill_logical_keys

            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = connect(db_path)
            migrate(conn)
            backfill_logical_keys(conn)
            conn.close()

        yield

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/events", events),
            Route("/healthz", healthz),
            Route("/admin/mark-read", admin_mark_new_since_read, methods=["POST"]),
            Route("/admin/projects/{project}/mark-read", admin_mark_read, methods=["POST"]),
            Route("/project/{project}", project_page),
            Route("/project/{project}/feature/{slug}", feature_page),
            Route("/doc/{document_id:int}", doc_shell),
            Route(
                "/doc/{document_id:int}/synthesis-response",
                post_synthesis_response,
                methods=["POST"],
            ),
            Route("/doc/{document_id:int}/comments", post_comments, methods=["POST"]),
            Route(
                "/api/documents/{project}/{feature}/{doc_type}/{instance:int}",
                put_document,
                methods=["PUT"],
            ),
            Route(
                "/api/documents/{project}/{feature}/{doc_type}/{instance:int}",
                get_document,
            ),
            Route(
                "/api/documents/{project}/{feature}/{doc_type}/{instance:int}/comments",
                get_document_comments,
            ),
            Route(
                "/api/documents/{project}/{feature}/{doc_type}/{instance:int}/comments/integrate",
                post_document_comments_integrate,
                methods=["POST"],
            ),
            Route(
                "/api/documents/{project}/{feature}/{doc_type}/{instance:int}/synthesis",
                get_document_synthesis,
            ),
            Route("/api/manifests/{doc_type}", get_manifest),
            Route("/api/projects", list_projects_handler),
            Route("/api/projects/{project}/features", list_features_handler),
            Route(
                "/api/projects/{project}/features/{feature}",
                get_feature_handler,
                methods=["GET"],
            ),
            Route(
                "/api/projects/{project}/features/{feature}",
                create_feature_handler,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project}/features/{feature}/documents",
                list_documents_handler,
            ),
            Route(
                "/api/projects/{project}/features/{feature}/capture",
                capture_handler,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project}/features/{feature}/claim",
                claim_handler,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project}/features/{feature}/park",
                park_handler,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project}/features/{feature}/release",
                release_handler,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project}/features/{feature}/ship",
                ship_handler,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project}/features/{feature}/drop",
                drop_handler,
                methods=["POST"],
            ),
            Route(
                "/api/projects/{project}/features/{feature}/note",
                note_handler,
                methods=["POST"],
            ),
            Route("/retro-findings", post_retro_findings, methods=["POST"]),
            Route("/retro-findings", get_retro_findings),
            Route(
                "/retro-findings/{finding_id:int}/status",
                post_retro_finding_status,
                methods=["POST"],
            ),
            Mount("/static", StaticFiles(directory=STATIC_DIR)),
        ],
        lifespan=lifespan,
    )
    app.state.templates = templates
    app.state.db_path = db_path
    return app
