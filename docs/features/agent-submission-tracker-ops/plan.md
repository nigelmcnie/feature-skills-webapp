# agent-submission-tracker-ops

## Overview

Add the additive tracker API to the existing Starlette app: read-only **listing** of projects / features / a feature's documents, and typed **mutations** (`capture` / `claim` / `ship`) that write the `features` table directly, emit feature-level events, and trigger the existing SSE broadcast. A new `storage/tracker.py` holds both the read accessors (factored out of the existing project/feature page handlers so there's one source) and the mutation functions; a thin `web/tracker.py` exposes them as HTTP handlers following the established `web/submit.py` patterns. A one-off migration backfills existing `NULL`-status rows to `available`, and `upsert_feature` starts defaulting new rows to `available`, so the status invariant holds in code without a DB `CHECK`. This feature changes *no* walker behaviour; the walker-authority flip that makes these writes the sole source of truth is owned by `skills-api-cutover`.

## Key technical decisions

1. **One `storage/tracker.py` for read accessors + mutations; refactor the page handlers onto it**
  The listing endpoints and the existing `project_page` / `feature_page` need the same queries. Factor them into `storage/tracker.py` (per the requirements' Indicative notes) so the API and the pages share one implementation rather than duplicating SQL. Mutations live in the same module for cohesion. The connection-taking style matches `storage/inbox.py` / `storage/read_state.py`.
  ```python
  # storage/tracker.py — read accessors (connection-taking, like inbox.py)
  import sqlite3

  FEATURE_STATUSES: tuple[str, ...] = ("available", "in_progress", "done")

  def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
      return conn.execute("SELECT name FROM projects ORDER BY name").fetchall()

  def get_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
      return conn.execute("SELECT id, name FROM projects WHERE name=?", (name,)).fetchone()

  def list_features(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
      # Same query project_page uses, plus notes; one source of truth.
      return conn.execute(
          "SELECT f.slug, f.status, f.owner, f.notes, "
          "  (SELECT MAX(e.created_at) FROM events e "
          "   JOIN documents d ON e.document_id = d.id "
          "   WHERE d.feature_id = f.id AND d.status = 'active') AS last_activity "
          "FROM features f WHERE f.project_id = ? ORDER BY f.status, f.slug",
          (project_id,),
      ).fetchall()

  def get_feature(conn: sqlite3.Connection, project: str, slug: str) -> sqlite3.Row | None:
      return conn.execute(
          "SELECT f.id, f.slug, f.status, f.owner, f.notes, p.name AS project "
          "FROM features f JOIN projects p ON f.project_id = p.id "
          "WHERE p.name = ? AND f.slug = ?",
          (project, slug),
      ).fetchone()

  def list_feature_documents(conn: sqlite3.Connection, feature_id: int) -> list[sqlite3.Row]:
      # Active docs only (excludes archived/missing); feature-scoped, so the
      # project-level tracker doc (feature_id IS NULL) can never appear.
      return conn.execute(
          "SELECT d.id, d.type, d.instance, d.logical_key, "
          "  (SELECT COALESCE(MAX(v.version_num), 0) FROM document_versions v "
          "   WHERE v.document_id = d.id) AS version "
          "FROM documents d WHERE d.feature_id = ? AND d.status = 'active' "
          "ORDER BY d.type, d.instance",
          (feature_id,),
      ).fetchall()
  ```
2. **REST listing routes; action-suffixed mutation routes — both under `/api/projects/...`**
  Mirror the existing `/api/...` namespace. Listing is plain REST; mutations are typed action suffixes (not a generic field-setter) so transition rules and event selection stay server-side. Feature segments here carry the real slug (not the `-` project sentinel used by the documents API).
  ```python
  # web/app.py — add to the routes=[...] list
  Route("/api/projects", list_projects_handler),
  Route("/api/projects/{project}/features", list_features_handler),
  Route("/api/projects/{project}/features/{feature}/documents", list_documents_handler),
  Route("/api/projects/{project}/features/{feature}/capture", capture_handler, methods=["POST"]),
  Route("/api/projects/{project}/features/{feature}/claim", claim_handler, methods=["POST"]),
  Route("/api/projects/{project}/features/{feature}/ship", ship_handler, methods=["POST"]),
  ```
3. **Explicit mutation contract via typed exceptions mapped to HTTP codes**
  Storage mutation functions raise typed exceptions; the handler maps them. **Only `capture` creates** (409 if the feature already exists); `claim`/`ship` on a missing feature → 404 (never a silent NULL-status create). A **redundant transition** (claim an already-in_progress feature, re-ship a done one) is an idempotent success with `changed=False` and *no* event. An **invalid transition** (claim a done feature, ship an available one) → 409. Status values validated against `FEATURE_STATUSES`.
  ```python
  # storage/tracker.py — mutation result + errors
  from dataclasses import dataclass

  class TrackerError(Exception): ...
  class FeatureNotFound(TrackerError): ...
  class FeatureExists(TrackerError): ...
  class InvalidTransition(TrackerError): ...

  @dataclass(frozen=True)
  class MutationResult:
      project: str
      slug: str
      status: str
      changed: bool   # False on idempotent no-op (no event emitted)

  def capture_feature(conn, *, project: str, slug: str,
                      notes: str | None, now: str) -> MutationResult: ...
  def claim_feature(conn, *, project: str, slug: str,
                    owner: str, now: str) -> MutationResult: ...
  def ship_feature(conn, *, project: str, slug: str,
                   outcome: str | None, now: str) -> MutationResult: ...
  ```
  Transition logic (claim shown; ship is the symmetric `in_progress → done`, reusing the `shipped` event):
  ```python
  def claim_feature(conn, *, project, slug, owner, now):
      feat = get_feature(conn, project, slug)
      if feat is None:
          raise FeatureNotFound(f"{project}/{slug}")
      if feat["status"] == "in_progress":
          return MutationResult(project, slug, "in_progress", changed=False)  # idempotent
      if feat["status"] != "available":
          raise InvalidTransition(f"cannot claim from {feat['status']!r}")
      conn.execute(
          "UPDATE features SET status='in_progress', owner=?, updated_at=? WHERE id=?",
          (owner, now, feat["id"]),
      )
      conn.execute(
          "INSERT INTO events (document_id, event_type, payload_json, created_at) "
          "VALUES (NULL, 'feature_claimed', ?, ?)",
          (json.dumps({"project": project, "slug": slug, "owner": owner}), now),
      )
      return MutationResult(project, slug, "in_progress", changed=True)
  ```
4. **Status invariant in code: backfill migration + `upsert_feature` default; no `CHECK`**
  A DB `CHECK` would need a full SQLite table rebuild (rejected — see requirements round 2). Instead: migration `0005` backfills existing `NULL` statuses (a certainty on the live DB), and `upsert_feature` — the create path used by document submit and the walker — defaults new rows to `available` on INSERT. `ON CONFLICT DO NOTHING` means existing rows are untouched (the walker's `_apply_tracker_rows` still overwrites status for tracker-parsed features). Together: no row is ever left status-less.
  ```sql
  -- storage/migrations/0005_feature_status_backfill.sql
  UPDATE features SET status = 'available' WHERE status IS NULL;

  INSERT INTO schema_version (version) VALUES (5)
  ```
  ```python
  # storage/walker.py — upsert_feature now seeds a valid status on insert
  def upsert_feature(conn, project_id, slug, now):
      conn.execute(
          "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
          "VALUES (?, ?, 'available', ?, ?) "
          "ON CONFLICT(project_id, slug) DO NOTHING",
          (project_id, slug, now, now),
      )
      return conn.execute(
          "SELECT id FROM features WHERE project_id=? AND slug=?", (project_id, slug)
      ).fetchone()["id"]
  ```
5. **Feature-level events + SSE, matching the existing pattern**
  Each successful (non-no-op) mutation inserts an `events` row with `document_id = NULL` and a `{project, slug, ...}` payload — the shape the walker's `shipped` event already uses — and the handler calls `request.app.state.broadcaster.broadcast()` so open tabs refresh. `ship` reuses the existing `'shipped'` event type, so the inbox's recently-shipped read needs no change; `capture` and `claim` use new types `'feature_captured'` / `'feature_claimed'` (ignored by existing reads, available for audit).
6. **Mutations are written-but-not-yet-authoritative against the live service**
  No walker change in this feature, so a re-walk still re-derives tracker rows from `features.html` and will overwrite a mutation for any feature still in that file. The endpoints are correct and unit-testable; the keystone "mutation survives a walk" test and the parse-retirement that makes them durable belong to `skills-api-cutover`. Do not add a test here that asserts survival across a walk.

## File structure

### New files

- `feature_skills_webapp/storage/tracker.py` — read accessors (`list_projects`, `get_project`, `list_features`, `get_feature`, `list_feature_documents`), mutation functions (`capture_feature`, `claim_feature`, `ship_feature`), `MutationResult`, the `TrackerError` hierarchy, and `FEATURE_STATUSES`.
- `feature_skills_webapp/web/tracker.py` — six HTTP handlers (3 GET listing, 3 POST mutations) following `web/submit.py` patterns.
- `feature_skills_webapp/storage/migrations/0005_feature_status_backfill.sql` — backfill `NULL` status → `available`.
- `feature_skills_webapp/storage/tracker_test.py` — storage read + mutation tests.
- `feature_skills_webapp/web/tracker_test.py` — handler tests.

### Modified files

- `feature_skills_webapp/web/app.py` — register the six new routes; import the handlers.
- `feature_skills_webapp/web/project_page.py` — call `tracker.get_project` / `tracker.list_features` instead of inline SQL. (It will now receive one extra column, `notes`, which it simply ignores — harmless.)
- `feature_skills_webapp/web/feature_page.py` — share **only** `tracker.get_feature`. **Do not** substitute `list_feature_documents` here: this page surfaces an `awaiting` flag and `archived` docs that the active-only accessor would drop, so its documents query stays inline. (The API's documents listing is intentionally active-only; the page is not.)
- `feature_skills_webapp/storage/walker.py` — `upsert_feature` seeds `status='available'` on insert. (Relocation of `upsert_feature`/`upsert_project` off the doomed walker module is left to `skills-api-cutover`.)

## Phase 1 — Listing over the API

### What's built

The read substrate: `storage/tracker.py` read accessors, three GET endpoints, and a refactor of the two page handlers onto the shared accessors. No mutations, no schema change — lowest blast radius, ships value immediately, independent of any walker change.

### Endpoints & response shapes

```python
# GET /api/projects
{"projects": [{"name": "feature-skills-webapp"}, ...]}

# GET /api/projects/{project}/features        404 if project unknown
{"project": "feature-skills-webapp",
 "features": [{"slug": "doc-view", "status": "done",
               "owner": "Nigel", "notes": "..."}, ...]}

# GET /api/projects/{project}/features/{feature}/documents
#   404 only if the feature row is unknown; [] when the feature exists but has no docs
{"project": "...", "feature": "doc-view",
 "documents": [{"doc_type": "requirements", "instance": 1,
                "logical_key": "feature-skills-webapp/doc-view/requirements/1",
                "version": 3, "document_id": 42, "url": "/doc/42"}, ...]}
```

### Handler pattern (verbatim from `web/submit.py`)

```python
# web/tracker.py
async def list_features_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    name = request.path_params["project"]
    with request_conn(request.app) as conn:
        proj = get_project(conn, name)
        if proj is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        feats = list_features(conn, proj["id"])
    return JSONResponse({
        "project": name,
        "features": [
            {"slug": r["slug"], "status": r["status"],
             "owner": r["owner"], "notes": r["notes"]}
            for r in feats
        ],
    })
```

The accessors return raw rows; the handler assembles the response (renames `type → doc_type`, adds `url`). 404 keys on the *feature* row (via `get_feature`); a captured-but-undocumented feature returns `200` with `"documents": []`.

```python
async def list_documents_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]
    with request_conn(request.app) as conn:
        feat = get_feature(conn, project, slug)
        if feat is None:
            return JSONResponse({"error": "feature not found"}, status_code=404)
        docs = list_feature_documents(conn, feat["id"])
    return JSONResponse({
        "project": project, "feature": slug,
        "documents": [
            {"doc_type": r["type"], "instance": r["instance"],
             "logical_key": r["logical_key"], "version": r["version"],
             "document_id": r["id"], "url": f"/doc/{r['id']}"}
            for r in docs
        ],
    })
```

### Refactor

Move the features-by-project SQL out of `project_page` into `tracker.list_features` and have the page call it (a clean substitution — it ignores the extra `notes` column). For `feature_page`, share only `tracker.get_feature`; leave its documents query inline (it needs the `awaiting` flag and archived rows the active-only accessor drops). Phase 1 is behaviour-preserving for the pages — their rendered output and existing tests must stay green unchanged.

### Tests (`storage/tracker_test.py`, `web/tracker_test.py`)

- Storage: `list_projects` ordering; `list_features` returns status/owner/notes; `list_feature_documents` returns only `active` docs (seed an `archived` and a `missing` doc, assert excluded) and excludes the project-level tracker doc (`feature_id IS NULL`); version is `MAX(version_num)`; empty cases.
- Handlers: 200 happy paths with exact shapes; 404 on unknown project / unknown feature; `200 + []` for a feature with no docs; 503 when `db_path` is None (construct app without a DB, as existing handler tests do).
- Regression: existing `project_page_test.py` / `feature_page_test.py` still pass after the refactor (do not modify their assertions).

### MR chain

One MR titled `feat(agent-submission-tracker-ops): phase 1 — tracker listing API`.

## Phase 2 — Typed mutations + status invariant

### What's built

The three mutation functions and their POST endpoints, the `0005` backfill migration, and the `upsert_feature` status default. Mutations write the `features` table, emit feature-level events (transition-gated), and broadcast SSE.

### Mutation contract (handler mapping)

```python
# web/tracker.py
async def claim_handler(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["feature"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    owner = body.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return JSONResponse({"error": "'owner' must be a non-empty string"}, status_code=400)

    try:
        with request_conn(request.app) as conn, transaction(conn):
            result = claim_feature(conn, project=project, slug=slug,
                                   owner=owner, now=now_iso())
    except FeatureNotFound:
        return JSONResponse({"error": "feature not found"}, status_code=404)
    except InvalidTransition as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    if result.changed:
        request.app.state.broadcaster.broadcast()
    return JSONResponse({"project": result.project, "slug": result.slug,
                         "status": result.status, "changed": result.changed})
```

Body contracts: `claim` requires `owner` (non-empty string); `ship` takes optional `outcome` (string → written to `notes`); `capture` takes optional `notes` (string). Non-string values → 400, mirroring `submit.py`'s `actor` guard. `capture` maps `FeatureExists` → 409. Broadcast only when `changed` (a no-op shouldn't churn open tabs).

### Capture & ship specifics

```python
def capture_feature(conn, *, project, slug, notes, now):
    project_id = upsert_project(conn, project, now)
    existing = conn.execute(
        "SELECT 1 FROM features WHERE project_id=? AND slug=?", (project_id, slug)
    ).fetchone()
    if existing is not None:
        raise FeatureExists(f"{project}/{slug}")
    conn.execute(
        "INSERT INTO features (project_id, slug, status, notes, created_at, updated_at) "
        "VALUES (?, ?, 'available', ?, ?, ?)",
        (project_id, slug, notes, now, now),
    )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'feature_captured', ?, ?)",
        (json.dumps({"project": project, "slug": slug}), now),
    )
    return MutationResult(project, slug, "available", changed=True)

def ship_feature(conn, *, project, slug, outcome, now):
    feat = get_feature(conn, project, slug)
    if feat is None:
        raise FeatureNotFound(f"{project}/{slug}")
    if feat["status"] == "done":
        return MutationResult(project, slug, "done", changed=False)   # idempotent
    if feat["status"] != "in_progress":
        raise InvalidTransition(f"cannot ship from {feat['status']!r}")
    # outcome (when provided) writes the single notes column; omit → leave notes as-is
    if outcome is not None:
        conn.execute("UPDATE features SET status='done', notes=?, updated_at=? WHERE id=?",
                     (outcome, now, feat["id"]))
    else:
        conn.execute("UPDATE features SET status='done', updated_at=? WHERE id=?",
                     (now, feat["id"]))
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'shipped', ?, ?)",   # reuse: inbox recently-shipped reads this
        (json.dumps({"project": project, "slug": slug}), now),
    )
    return MutationResult(project, slug, "done", changed=True)
```

### Migration & upsert default

Add `0005_feature_status_backfill.sql` (decision 4). The `migrate()` runner splits on `;` — the single `UPDATE` plus the `schema_version` insert are clean plain DDL, no embedded semicolons. Update `upsert_feature` to seed `status='available'` on INSERT (decision 4).

### Tests (`storage/tracker_test.py`, `web/tracker_test.py`)

- Storage mutations: `capture` creates an `available` row + `feature_captured` event; `capture` on an existing slug raises `FeatureExists`. `claim` available→in_progress sets owner + emits `feature_claimed`; claim of already-in_progress returns `changed=False` and emits **no** event — assert via `SELECT COUNT(*) FROM events` before/after that the count is unchanged (this is the transition-gate test; counting the *events* rows, not just `changed`, is what makes it go red if the early-return gate is removed). Claim of a `done` feature raises `InvalidTransition`; claim of a missing feature raises `FeatureNotFound`. `ship` in_progress→done writes `notes` + emits `shipped`; re-ship is a no-op (events count unchanged); ship of an `available` feature raises `InvalidTransition`.
- Rejected-transition side-effects: after an `InvalidTransition` (e.g. ship an `available` feature), assert the feature's `status` is unchanged *and* no event row was added — pins that the rejection happens before any write, guarding a future reorder.
- Invariant: migration backfills a pre-seeded `NULL`-status row to `available`; `upsert_feature` on a brand-new slug yields `status='available'` (not NULL).
- Handlers: each endpoint's 200 shape; 400 (bad JSON / missing or non-string `owner`); 404 (claim/ship missing feature); 409 (invalid transition; capture existing); 503 (no DB). Assert `broadcaster.broadcast()` is invoked on a changed mutation and not on a no-op (spy on `app.state.broadcaster` as existing tests do, or assert via a fake).

### MR chain

One MR titled `feat(agent-submission-tracker-ops): phase 2 — tracker mutations + status invariant`.

## Verification

Run the full QC suite from `CLAUDE.md` (these are the real gates; `uv run pytest` is the whole suite under xdist + pytest-socket):

- `uv run ruff format --check .` (expect: no reformatting needed)
- `uv run ruff check .` (expect: no errors)
- `uv run ty check .` (expect: no type errors)
- `uv run pytest` (expect: all pass, including the new `storage/tracker_test.py` and `web/tracker_test.py`, and the unchanged `project_page_test.py` / `feature_page_test.py`)

End-to-end smoke against a redeployed service (per `CLAUDE.md`, code changes require `uv tool install --editable . --reinstall && systemctl --user restart feature-skills-webapp` first — testing against the stale running service shows old behaviour). *(Note: requires the running service; perform manually.)*

```bash
# after restart — listing
curl -fsS http://127.0.0.1:8800/api/projects
curl -fsS http://127.0.0.1:8800/api/projects/feature-skills-webapp/features
# mutation round-trip on a throwaway feature
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/zzz-smoke/capture \
  -H 'Content-Type: application/json' -d '{"notes":"smoke"}'
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/zzz-smoke/claim \
  -H 'Content-Type: application/json' -d '{"owner":"Nigel"}'
# expect status transitions available -> in_progress in the responses.
# NOTE: a subsequent walk re-deriving features.html will revert this row
# (expected until skills-api-cutover; do not treat as a failure).
```

## QC

Follow `CLAUDE.md` § "QA / quality control" before each commit: `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, `uv run pytest` — all must pass. If `pyproject.toml` is unchanged (it is — no new deps), no reinstall is needed for tests; only the live service needs a restart to reflect code changes.

## Checklist

### Phase 1: Listing

- Create `storage/tracker.py` with `FEATURE_STATUSES` and read accessors: `list_projects`, `get_project`, `list_features`, `get_feature`, `list_feature_documents` (active-only, feature-scoped, version = MAX(version_num)).
- Refactor `web/project_page.py` onto `list_features`; share only `get_feature` into `web/feature_page.py` (leave its documents query inline so the awaiting flag + archived grouping survive). Behaviour-preserving — page output unchanged.
- Create `web/tracker.py` with the three GET handlers (503/404/empty semantics) and register the three GET routes in `web/app.py`.
- Write `storage/tracker_test.py` (read accessors: ordering, active-only exclusion of archived/missing, tracker-doc exclusion, version, empties) and `web/tracker_test.py` (200 shapes, 404, 200+[], 503).
- Confirm existing `project_page_test.py` / `feature_page_test.py` still pass unchanged; run full QC; open the Phase 1 MR.

### Phase 2: Mutations + status invariant

- Add `storage/migrations/0005_feature_status_backfill.sql` (backfill `NULL` status → `available`; bump `schema_version` to 5).
- Update `upsert_feature` in `storage/walker.py` to seed `status='available'` on INSERT (ON CONFLICT DO NOTHING unchanged).
- Add `MutationResult`, the `TrackerError` hierarchy, and `capture_feature` / `claim_feature` / `ship_feature` to `storage/tracker.py` (transition-gated, feature-level events, ship reuses `shipped`).
- Add the three POST handlers to `web/tracker.py` (body validation → 400, exception mapping → 404/409, broadcast only when changed) and register the three POST routes in `web/app.py`.
- Extend tests: storage mutations (happy, no-op-emits-no-event, invalid-transition, not-found, capture-exists, capture-sets-available, ship-emits-shipped), the migration backfill, and `upsert_feature` default; handler tests (400/404/409/503, broadcast-on-change-only).
- Run full QC; open the Phase 2 MR.
