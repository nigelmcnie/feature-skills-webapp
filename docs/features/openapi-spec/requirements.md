# openapi-spec — Requirements

## Summary

The webapp already exposes a public HTTP API of **21 `/api/*` routes** (across roughly 18 distinct paths) — reading and writing feature documents, fetching per-doc-type manifests, and driving the project/feature lifecycle (create, claim, park, release, ship, drop, note, suggested-order, list, get). Today nothing describes that surface in a machine-readable way. Inside a feature workflow an agent learns the API from the feature-skills SKILL.md files; out of workflow — ad-hoc scripting, a one-off query, a human poking at it — the only "documentation" is reading the SQLite DB or the source.

This feature publishes an **OpenAPI specification** for that public API, served by the webapp itself. Hit `GET /openapi.json` and you get a self-describing document: every `/api` path, its methods, its path/query parameters, and the shape of its request and response bodies. The path-and-method inventory is *derived from the running app's route table*, so it cannot silently drift from the code; curated summaries and light request/response detail are layered on top of that derived skeleton.

Concrete example: an agent working outside the feature workflow wants to list a project's features. Instead of opening the DB, it fetches `/openapi.json`, finds `GET /api/projects/{project}/features` with its parameters and response shape described, and calls it directly. One self-describing front door replaces DB spelunking.

## Scope

In scope: an OpenAPI document covering the webapp's **public API namespace** — the `/api/*` routes (21 operations across ~18 path-items). That is the surface agents and tooling are meant to call:

- **Documents** — `GET`/`PUT /api/documents/{project}/{feature}/{doc_type}/{instance}`, plus `.../comments`, `.../comments/integrate`, `.../synthesis`, and `.../synthesis/wait`.
- **Manifests** — `GET /api/manifests/{doc_type}`.
- **Projects & features** — `/api/projects`, `/api/projects/{project}`, `.../suggested-order`, `.../features`, `.../features/{feature}` and its lifecycle verbs (`documents`, `claim`, `park`, `release`, `ship`, `drop`, `note`).

The document is served over HTTP at the stable path `GET /openapi.json`, so a consumer needs nothing but the base URL to discover the whole API.

## Vision

Anyone — agent or human — pointed at the running webapp can fetch one URL and learn the entire public API from the service itself, with the description guaranteed to track the code rather than lag behind it.

## Non goals

- **No change to the trust model.** The service stays localhost-only, no-auth, single-machine. Publishing a spec documents the existing surface; it does not add authentication, networking, or a hosted/remote deployment.
- **Not the internal/HTML surface.** Server-rendered pages (`/`, `/doc/{id}`, `/project/...`), admin endpoints (`/admin/...`), the SSE stream (`/events`), `/healthz`, retro-findings, and the browser-only form-post routes (`/doc/{id}/comments`, `/doc/{id}/synthesis-response`) are out of scope. The spec describes the `/api/*` namespace that external callers use. `/openapi.json` itself lives outside `/api`, so it does not document itself and is not covered by the parity test.
- **No rendered UI.** A browser docs page (Swagger/Redoc at `/docs`) is out of scope — the served JSON spec is the deliverable, and any OpenAPI viewer can render it. (Considered as a deferrable phase in review round 1 and dropped from scope entirely.)
- **No external publishing.** The spec is not pushed to any registry or third-party catalogue. (Wiring a pointer into the global CLAUDE.md is handled separately by Nigel, downstream of this feature.)

## User stories

1. As an agent working outside the feature workflow
  I want to fetch a machine-readable description of the API from the running service
  I need to read a plan doc for a feature but I'm not in the feature-skills workflow, so I have no SKILL.md guidance to hand. I GET

  , find the

  entry with its path parameters described, and call it — instead of opening the SQLite file to reverse-engineer the schema.
2. As Nigel, exploring the API
  I want a description I can load into any OpenAPI viewer to browse the endpoints
  I point an OpenAPI viewer (or just curl) at

  , expand the projects/features section, and read what

  expects in its body and returns — without reading

  .
3. As a maintainer adding a new route
  the spec to reflect the new route automatically, and to be told if I've left it undocumented
  I add a new

  route to

  . Because the inventory is derived from the route table, the path appears in

  immediately; a test

  if the route has no curated summary, so the spec can't quietly fall out of sync with the code.

## Technical approach

Build the spec's path/method inventory by walking the live application's route table (Starlette `Route` objects carry their path template, HTTP methods, and endpoint), filtered to the `/api/*` namespace. This makes the inventory a function of the code: a route added or removed in `app.py` changes the spec with no separate edit. **What the route table yields is deliberately thin** — paths, methods, and *path* parameters. Query parameters (e.g. `dry_run`, `q`, `status`) are read inside handlers, not declared on routes, and request/response bodies and status codes aren't on routes either — so all of those are supplied by the curated layer, not derived.

Over that derived skeleton, layer a **curated metadata table** — per-operation summary, description, and (in later detail) request/response shapes — authored from the handlers in `web/submit.py` (documents, manifests) and `web/tracker.py` (projects, features) but kept as reviewable data rather than scattered through the handlers.

Serve the assembled document at `GET /openapi.json`, generated from the current route table so it reflects the running app. The document's `servers` block is sourced from an **explicit, configurable public base URL** (a new config value) rather than echoing the raw bind host — so it stays a client-usable, reachable URL and remains correct if a reverse proxy is placed in front later; it defaults to the localhost bind. The top-level description states the localhost / no-auth / single-machine reality so a consumer doesn't mistake it for a remote authenticated API. `info.version` is populated from the package version. The document targets **OpenAPI 3.1** (aligns with JSON Schema).

How much request/response schema to model is a dial: phase 1 establishes the skeleton with a required summary per operation, path parameters, and response presence; richer detail is layered incrementally in phase 2. The whole change is additive — a new module, a new route, a config value, and a curated data table — and preserves the existing trust model untouched.

## Testing

The load-bearing test is **route-table ↔ spec parity**: assert that every `/api/*` operation on the live app appears in the generated spec, and that every spec operation still maps to a real route. This is what makes the anti-drift claim true rather than aspirational — adding an `/api` route without documenting it turns the suite red (a hard build failure, not a warning). To keep that cost low, the phase-1 coverage bar is a single required field: a one-line `summary` per operation.

- The generated document is valid OpenAPI 3.1 (validates against an off-the-shelf validator — a test-scoped dependency; see Alternatives).
- `GET /openapi.json` returns 200 with the expected content type.
- The `servers` block reflects the configured public base URL (and defaults to the localhost bind when unset) rather than a hardcoded value.
- Coverage: any operation missing its required summary is a test failure (drives the "you left it undocumented" story).
- **Phase 2 (option):** a golden-response test that pins curated response schemas against real responses from the running app, extending the anti-drift guarantee to response bodies.

All under the existing QA gate: `uv run ruff format/check`, `uv run ty check`, `uv run pytest`.

## Alternatives

1. Hand-maintained static openapi.jsonSimplest possible optionRejected: it drifts from the code the moment a route changes, which is precisely the failure this feature exists to prevent.
2. Starlette's built-in SchemaGenerator (YAML in endpoint docstrings)starlette.schemasConsidered. It does derive from routes, but couples the spec to per-endpoint docstrings — scattering the curated detail across handlers and mixing prose YAML into code — rather than keeping it as a reviewable data table. Worth weighing against a hand-rolled route walk during planning.
3. A framework/dependency that generates the spec (FastAPI-style, apispec, etc.)Third-party librariesRejected for now: adds a *runtime* dependency and, in FastAPI's case, a framework migration, for a surface small enough to describe with a route walk plus a curated table. Note the chosen path is not literally zero-dependency — validating 3.1 in tests adds one *test-scoped* validator dependency (e.g. openapi-spec-validator); the constraint we're keeping is no new runtime/framework dependency.

## Delivery phases

### Phase 1 — Derived spec + served endpoint + parity test

Walk the route table, filter to `/api/*`, emit a valid OpenAPI 3.1 document with paths, methods, typed path parameters, per-operation summaries, and response presence, plus the `servers` block (from the configurable public base URL) and the trust-model description. Serve it at `GET /openapi.json`. Land the route-table ↔ spec parity test and the summary-coverage test (both hard build failures) and the valid-3.1 validation. Delivers standalone value: a complete, valid, drift-proof inventory of the API is fetchable from the service.

### Phase 2 — Curated request/response detail

Flesh out the per-operation curated layer: query parameters, request-body shapes, response-body shapes, and richer descriptions for the operations where it matters most (document PUT/GET, the lifecycle verbs). For document endpoints, reference the manifest endpoint (`GET /api/manifests/{doc_type}`) as the source of truth for section shapes rather than re-describing them, and document the `-` feature sentinel (project-level documents). Optionally add the golden-response test. Incremental — each operation can gain detail without disturbing the phase-1 skeleton.

## Indicative notes

Plan-stage detail worth carrying forward, not binding:

- Starlette exposes routes via `app.routes`; each `Route` has `.path`, `.methods`, `.endpoint`, and usefully `.path_format` (converter suffix already stripped, e.g. `/api/.../{instance}`) plus `.param_convertors` — prefer those over hand-translating `{instance:int}`.
- Two route-walk behaviours to handle: (a) GET routes report `methods = {'GET','HEAD'}` — filter out `HEAD` or the spec/parity test carry phantom operations; (b) several paths appear as multiple `Route` objects sharing one `path_format` with different methods (documents PUT+GET, projects GET+POST, features GET+POST) — merge them into one path-item with multiple operations.
- Generate at request time from the live route table rather than emitting a build artifact, so the served spec always matches the running app (and the parity test exercises the same path).
- The `servers` URL comes from a new configurable public base URL (defaulting to the localhost bind, `config.host()`/`config.port()`); `create_app` doesn't currently receive host/port, so the generator likely reads config directly.
- Query params live in handlers, not on routes: `dry_run` in `put_document`, `q`/`status` in `list_features_handler` — curate these in phase 2.
- Error-response conventions are a shared contract (503 when db unconfigured; 400/404/409 with self-describing bodies per `docs/transitions/api-coherence.md`) — curate, don't derive.
- `info.version` from the package version. OpenAPI 3.1 aligns with JSON Schema and is the chosen version.

## Design notes

Decisions from requirements review round 1:

- **Coverage is a hard CI failure**, not a warning — an undocumented `/api` route fails the build. Phase-1 bar kept low: only a one-line summary per operation is required.
- **Public surface committed:** served at `GET /openapi.json`, OpenAPI 3.1.
- **Rendered UI (Swagger/Redoc at `/docs`) dropped from scope entirely** — not carried as a deferred phase and not captured as a separate feature; may be re-requested later if wanted.
- **`servers` block from an explicit configurable public base URL** (default = localhost bind), chosen over echoing the raw bind host so the URL stays client-usable and survives a future reverse proxy — rather than the narrower "localhost fallback for 0.0.0.0" originally proposed.
- **Only paths/methods/path-params are route-derived;** query params, bodies, and response codes are curated (corrects an over-broad "derived" claim).
- **Phase-2 source-of-truth reuse:** reference the manifest endpoint for document body shapes; optional golden-response test to keep phase-2 response schemas from drifting.
