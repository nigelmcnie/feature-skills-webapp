# openapi-spec — Context

## Problem space

The webapp exposes a public HTTP API of roughly 25 routes (see `feature_skills_webapp/web/app.py`) but ships no machine-readable description of it. The surface spans three groups: document CRUD and collaboration (`GET/PUT /api/documents/{project}/{feature}/{doc_type}/{instance}`, plus `.../comments`, `.../comments/integrate`, `.../synthesis`, `.../synthesis/wait`), manifests (`GET /api/manifests/{doc_type}`), and project/feature lifecycle (`/api/projects`, `/api/projects/{project}/features/...` with claim/park/release/ship/drop/note, suggested-order, list/get/create).

Agents driving a feature learn this API from the feature-skills SKILL.md files while *in-workflow*. But ad-hoc or out-of-workflow access has no front door — the fallback today is spelunking the SQLite DB directly. A served OpenAPI spec would give one self-describing entry point that documents the API on its own terms.

## Related work

Handlers already exist and are the source of truth for behaviour: `web/submit.py` (documents + manifests) and `web/tracker.py` (projects + features). The route table in `web/app.py` is the canonical path/method inventory. The manifest endpoint (`GET /api/manifests/{doc_type}`) already returns machine-readable per-doc-type section shapes, so there is precedent for the service describing its own structure over HTTP.

The feature-skills SKILL.md docs currently carry the human-facing description of these endpoints; an OpenAPI spec complements rather than replaces them, serving the out-of-workflow / tooling audience.

## Constraints

- **Derive, don't hand-maintain.** Design lean (for the plan stage, not binding): build the path/method inventory from the app's route table rather than a hand-written list, so the spec can't drift from the code. Layer curated summaries and light request/response detail on top of the derived skeleton.
- **Trust model unchanged.** localhost / no-auth. The service binds `127.0.0.1` by default (host configurable) and in practice runs on a single machine — the spec should be explicit about base URL and reachability rather than implying a networked, authenticated service.
- **Serving surface.** A stable path such as `GET /openapi.json`; optionally a Swagger/Redoc UI at `/docs`.
- **QA gate before commit** (per CLAUDE.md): `uv run ruff format` / `ruff check`, `uv run ty check`, `uv run pytest`.

## Links

- `feature_skills_webapp/web/app.py` — route table (canonical path/method inventory)
- `feature_skills_webapp/web/submit.py` — documents + manifests handlers
- `feature_skills_webapp/web/tracker.py` — projects + features handlers

## Open questions

- How much request/response schema detail to model — full JSON schemas for bodies, or a lighter summary layer over the derived path/method skeleton?
- Ship a bundled Swagger/Redoc UI at `/docs`, or serve `/openapi.json` only and leave rendering to external tools?
- Generate the spec on the fly from the live route table at request time, or produce it as a build/export artifact?
- How should the spec express the localhost / no-auth / single-machine reality (servers block, description) so consumers don't assume a remote authenticated API?
