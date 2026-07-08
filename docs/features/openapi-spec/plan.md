# openapi-spec — Plan

## Overview

The webapp serves a public HTTP API — 21 routes under `/api` — but nothing describes it in a machine-readable way, so anyone using it outside the feature workflow has to read the database or the source. This plan adds a served **OpenAPI specification**: fetch `GET /openapi.json` and you get a document listing every `/api` path, its methods, and its parameters, with human-written summaries on top.

The trick that keeps it honest: the list of paths and methods is *read from the running app's own routing table* rather than typed out by hand, so it can't fall out of step with the code. A test fails the build if a route exists without a written summary, so the description can never quietly rot. We build it in two increments — first the always-correct skeleton and the endpoint that serves it, then richer request/response detail — and we change nothing about how the service is trusted or reached.

## Key decisions

### Derive the inventory from the route table; curate the rest

A new module walks `app.routes`, keeps the `/api` routes, and turns them into OpenAPI path-items. Only **paths, methods, and path parameters** come from the routing table. Everything else (summaries, query params, bodies, status codes) comes from a curated data table keyed by `(method, path_format)`.

```
# feature_skills_webapp/web/openapi.py
OPENAPI_VERSION = "3.1.0"

# Curated per-operation metadata, keyed by (HTTP method, Starlette path_format).
# Phase 1 requires only "summary"; phase 2 adds parameters/requestBody/responses.
API_METADATA: dict[tuple[str, str], dict] = {
    ("GET", "/api/projects"): {"summary": "List all projects"},
    ("POST", "/api/projects/{project}"): {"summary": "Create a project"},
    # … one entry per /api operation …
}

def build_spec(routes: list, *, base_url: str, version: str) -> dict:
    """Assemble the OpenAPI 3.1 document from the live route table + curated metadata."""

```

### Generate at request time, not as a build artifact

The handler calls `build_spec(request.app.routes, …)` on each request. The served document therefore always matches the running app, and the parity test exercises the exact same code path.

### Path-parameter typing via Starlette's own convertors

Use `route.path_format` (already `/api/.../{instance}`, converter suffix stripped) as the OpenAPI path key, and `route.param_convertors` for types — no hand-parsing of `{instance:int}`. Map convertor classes to JSON-Schema types:

```
from starlette.convertors import IntegerConvertor, FloatConvertor

def _param_type(conv) -> str:
    if isinstance(conv, IntegerConvertor): return "integer"
    if isinstance(conv, FloatConvertor): return "number"
    return "string"  # StringConvertor, PathConvertor, UUIDConvertor, …

```

### Two route-walk gotchas, handled explicitly

(1) GET routes report `methods = {"GET", "HEAD"}` — drop `HEAD` (and `OPTIONS`) so the spec/parity carry no phantom operations. (2) Several paths appear as two `Route` objects sharing one `path_format` but different methods (documents PUT+GET, projects GET+POST, features GET+POST). Group by `path_format` and merge into one path-item with multiple operations.

### servers block from a configurable public base URL

Add `config.public_base_url()`. If `FEATURE_SKILLS_WEBAPP_PUBLIC_URL` is set, use it verbatim (survives a reverse proxy in front); otherwise derive from host/port, mapping a wildcard bind to a reachable loopback:

```
def public_base_url() -> str:
    override = os.environ.get("FEATURE_SKILLS_WEBAPP_PUBLIC_URL")
    if override:
        return override.rstrip("/")
    h = host()
    if h in ("0.0.0.0", "::"):  # not client-usable — advertise loopback
        h = "127.0.0.1"
    return f"http://{h}:{port()}"

```

### info.version from package metadata

Read the installed version with a static fallback so it never raises when run from a non-installed tree:

```
from importlib.metadata import PackageNotFoundError, version as _pkg_version

def _api_version() -> str:
    try:
        return _pkg_version("feature-skills-webapp")
    except PackageNotFoundError:
        return "0.0.0"

```

### Validation dependency is test-scoped

Add `openapi-spec-validator` to the `dev` dependency group only — no new runtime dependency. **Contingency:** if it won't resolve under Python 3.14 and the `exclude-newer = "P14D"` window, fall back to validating the document against the published OpenAPI 3.1 meta-schema via `jsonschema` (already resolvable), or a structural assertion — the point is a real validity check, not the specific package.

### The coverage test is the anti-drift guard — parity is structural

Because the spec is *generated from* the same route walk the parity test uses, route→spec parity is near-tautological: `build_spec` emits an operation (with an empty summary) for every route method whether or not it has an `API_METADATA` entry, so route→spec parity can't detect an *undocumented* route. The real guard is the **coverage test**: every walked `(method, path_format)` must have an `API_METADATA` entry with a non-empty summary — that is what fails the build when a new `/api` route lands uncurated. Keep route→spec and no-phantom parity as complementary structural checks: they and coverage cover each other's blind spots (e.g. if `build_spec` were ever changed to *skip* ops lacking metadata, coverage would go blind and route→spec would catch the gap). Add a comment so neither test is later deleted as "redundant".

### Route filter is Route-only (known limitation)

The walk filters `isinstance(r, Route) and r.path.startswith("/api")`. A future `Mount` or `WebSocketRoute` under `/api` would be silently skipped — neither documented nor caught by the tests. Fine for the current surface (the only `Mount` is `/static`, outside `/api`); note it as a latent gap in the module.

## Data model

No persistence or schema changes. The spec is computed in memory from the route table on each request; nothing is stored, and no migration is involved.

## Contract

### GET /openapi.json

New top-level route (outside `/api`, so it does not appear in its own spec and is not covered by the parity test).

- **Request:** `GET /openapi.json`, no parameters, no body.
- **Response:** `200`, `Content-Type: application/json`, body = the OpenAPI 3.1 document. Served regardless of whether the DB is configured (it describes routes, not data), so unlike the `/api` handlers it does *not* return 503 when `db_path is None`.

Top-level document shape (phase 1):

```
{
  "openapi": "3.1.0",
  "info": {"title": "feature-skills-webapp API", "version": "0.1.0",
           "description": "Localhost-only, no-auth, single-machine service. …"},
  "servers": [{"url": "http://127.0.0.1:8800",
               "description": "Configured public base URL (default: localhost bind)"}],
  "paths": {
    "/api/projects/{project}/features": {
      "get": {"summary": "List a project's features",
              "parameters": [{"name": "project", "in": "path", "required": true,
                              "schema": {"type": "string"}}]}
    }
  }
}

```

## File structure

### Created

- `feature_skills_webapp/web/openapi.py` — route walk, curated `API_METADATA`, `build_spec()`, and the `openapi_json` handler.
- `feature_skills_webapp/web/openapi_test.py` — parity, coverage, validity, servers, and endpoint tests.

### Modified

- `feature_skills_webapp/web/app.py` — import `openapi_json`; add `Route("/openapi.json", openapi_json)` (top-level, near `/healthz`).
- `feature_skills_webapp/config.py` — add `public_base_url()`.
- `pyproject.toml` — add `openapi-spec-validator` to the `dev` group.

## Verification

Run from the repo root. These fail loudly if the feature is absent or broken.

```
# Full QA gate (per CLAUDE.md)
uv run ruff format --check .
uv run ruff check .
uv run ty check .
uv run pytest

# The new suite specifically (parity, coverage, validity, endpoint, servers)
uv run pytest feature_skills_webapp/web/openapi_test.py -v

# The served endpoint on the running service (start it first if needed):
#   uv run feature-skills-webapp   # in another shell
curl -fsS http://127.0.0.1:8800/openapi.json | jq '.openapi, (.paths | keys | length)'
# expect: "3.1.0" then the /api path-item count (~18)

# Prove the anti-drift guard actually bites: temporarily add a dummy /api route
# to app.py WITHOUT an API_METADATA entry, run the suite, and confirm the
# COVERAGE test fails (that is the guard — route→spec parity would still pass,
# since build_spec emits an empty-summary op for it) — then revert.

```

## Qc

Follow the QC steps in `CLAUDE.md` at implementation time — currently: `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, `uv run pytest`. All must pass before committing. Because `pyproject.toml` changes (new dev dependency), run `uv sync` before the test run so `openapi-spec-validator` is installed.

## Checklist

### Phase 1: Derived spec + endpoint + parity/coverage tests

- Add `public_base_url()` to `config.py` (env override; wildcard-bind → loopback fallback).
- Add `openapi-spec-validator` to the `dev` group in `pyproject.toml`; run `uv sync`.
- Create `web/openapi.py`: convertor→type map, `_api_version()`, and `build_spec()` (filter /api, drop HEAD/OPTIONS, merge multi-method by `path_format`, typed path params).
- Write the curated `API_METADATA` table with a one-line summary for all 21 `/api` operations.
- Add the `openapi_json` handler and register `Route("/openapi.json", openapi_json)` in `app.py`.
- Write `openapi_test.py`: coverage (THE anti-drift guard — summary required), structural parity + no-phantom (complementary), validity, endpoint (200/exact json content-type, works with `create_app(None)`), servers (override + 0.0.0.0 fallback), path-param typing, version-is-non-empty.
- Run the full QA gate; confirm the *coverage* test fails when a dummy undocumented /api route is added, then revert the dummy.

### Phase 2: Curated request/response detail

- Extend `API_METADATA` with query params (`dry_run`, `q`, `status`) and request bodies for document PUT and the lifecycle verbs.
- Reference `GET /api/manifests/{doc_type}` as source-of-truth for document section shapes; document the `-` feature sentinel.
- Add `responses` (shared error conventions + success shapes) to the curated operations.
- Have `build_spec()` merge curated parameters/requestBody/responses onto derived operations; keep phase-1 tests green.
- (Optional) Add the golden-response test pinning curated response schemas against real `TestClient` responses.
- Run the full QA gate.

## Phase 1

**Build:** the route walk, the curated `API_METADATA` table with a one-line `summary` for all 21 `/api` operations, `build_spec()` producing a valid OpenAPI 3.1 document (paths, methods, typed path parameters, per-operation summary, top-level `info`/`servers`/`description`), the `config.public_base_url()` helper, and the `GET /openapi.json` route. **Scope note:** phase 1 does *not* emit `responses` — the Operation Object's `responses` is optional in OpenAPI 3.1, so the document is still valid, and all response detail is deferred to phase 2. (This is the summary-only phase-1 bar; the requirements' "response presence" phrasing is aligned to this.)

**Files:** create `web/openapi.py` + `web/openapi_test.py`; modify `app.py`, `config.py`, `pyproject.toml`.

**Key logic:**

```
def build_spec(routes, *, base_url, version):
    from starlette.routing import Route
    ops_by_path: dict[str, dict] = {}
    for r in routes:
        if not isinstance(r, Route) or not r.path.startswith("/api"):
            continue
        methods = {m for m in (r.methods or []) if m not in ("HEAD", "OPTIONS")}
        params = [{"name": n, "in": "path", "required": True,
                   "schema": {"type": _param_type(c)}}
                  for n, c in r.param_convertors.items()]
        item = ops_by_path.setdefault(r.path_format, {})
        for method in methods:
            meta = API_METADATA.get((method, r.path_format), {})
            op = {"summary": meta.get("summary", "")}
            if params: op["parameters"] = params
            item[method.lower()] = op
    return {"openapi": OPENAPI_VERSION, "info": {…}, "servers": [{"url": base_url, …}], "paths": ops_by_path}

```

**Tests** (`openapi_test.py`, using `TestClient(create_app(None))` and a direct `create_app(None).routes` walk):

- **Coverage — THE anti-drift guard:** walk the app's `/api` routes and, for each `(method, path_format)` (excluding HEAD/OPTIONS), assert an `API_METADATA` entry exists with a non-empty `summary`. This is the test that fails the build when a new `/api` route lands uncurated — verification step 7 targets *this* test.
- **Structural parity — every route emitted:** every walked `(path_format, method)` appears in the spec under that lowercased method. (Complements coverage; if `build_spec` is ever changed to skip metadata-less ops, this is the backstop — do not delete as redundant.)
- **No phantom operations:** every operation in the spec maps back to a real route/method.
- **Validity:** the generated document passes `openapi-spec-validator` (or the meta-schema fallback per Key decisions).
- **Endpoint:** `GET /openapi.json` → 200, exact `Content-Type: application/json`, parses, has `openapi == "3.1.0"`; works with `create_app(None)` (no 503).
- **servers:** with `FEATURE_SKILLS_WEBAPP_PUBLIC_URL` set, the block echoes it; unset with host `0.0.0.0`, the block advertises `127.0.0.1` (monkeypatch env).
- **Path-param typing:** `{instance}` is typed `integer`, `{project}` is `string`.
- **Test hygiene:** assert `info.version` is a non-empty string (don't hard-code `0.1.0` — it differs between installed and non-installed trees).

**Deliverable:** a complete, valid, drift-proof API inventory fetchable from the service. One MR.

## Phase 2

**Build:** extend `API_METADATA` so operations carry `parameters` (query), `requestBody`, and `responses`. `build_spec()` merges these curated fields onto the derived operations. Focus on the high-value operations first: document `PUT`/`GET` and the lifecycle verbs.

**Specifics to capture (from the handlers):**

- **Query params** (not on routes — read in handlers): `dry_run` on `PUT /api/documents/...` (`submit.py`); `q` and `status` on `GET /api/projects/{project}/features` (`tracker.py`).
- **Request bodies:** document PUT (`sections`/`body` + `actor` + optional `extra_css`); lifecycle verbs (`claim` needs `owner`; `note` needs `notes`; etc.); `suggested-order` PUT.
- **Doc-type body shapes:** for the documents endpoints, reference `GET /api/manifests/{doc_type}` as the source of truth for section shapes (a description pointer / link) rather than re-describing per-doc-type section keys inline.
- **The `-` feature sentinel:** document that a `{feature}` path segment of `-` addresses a project-level document (`submit.py`).
- **Responses:** the shared error conventions (503 when DB unconfigured; 400/404/409 with self-describing bodies per `docs/transitions/api-coherence.md`) plus the success shapes (fixed `JSONResponse` dict literals in the handlers).

**Tests:** extend coverage assertions for the operations that now require richer fields; keep the phase-1 parity/validity tests green. **Optional:** a golden-response test that drives representative operations against `TestClient` and asserts the real response matches the curated `responses` schema — extending the anti-drift guarantee to response bodies.

**Deliverable:** a fully-described API. One MR.
