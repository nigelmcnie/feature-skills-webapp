# tracker-feature-notes-update — Plan

## Overview

Add a single tracker mutation — `POST /api/projects/{project}/features/{feature}/note` — that sets the `notes` field on an existing feature. It mirrors the existing `capture`/`claim`/`ship` verbs exactly: a typed mutation in `storage/tracker.py`, a handler in `web/tracker.py`, a route in `web/app.py`, and tests beside the existing ones. Idempotent, 404 on a missing feature, 400 on a missing/non-string note, status-preserving, and broadcasts on change. No schema change. One MR.

The route path param is named `{feature}` to match every sibling tracker route; the handler reads `request.path_params["feature"]` into a local `slug`. Use `{feature}` in the route literal — not `{slug}`.

## Key decisions

1. **New action-style POST verb, not PATCH**
  Register `POST /api/projects/{project}/features/{feature}/note` alongside the other tracker verbs. Body `{"notes": "…"}`. Consistent with `capture`/`claim`/`ship`/`drop`, all of which are action-style POSTs. The path param literal is `{feature}` (matching the sibling routes); the handler reads it via `path_params["feature"]` and assigns it to a local `slug`.
2. **Typed storage mutation mirroring the siblings**
  Returns a `MutationResult`; idempotent no-op when the note is unchanged.
  ```
  def update_feature_note(
      conn: sqlite3.Connection,
      *,
      project: str,
      slug: str,
      notes: str,
      now: str,
  ) -> MutationResult:
      slug = slugify(slug)
      feat = get_feature(conn, project, slug)
      if feat is None:
          raise FeatureNotFound(f"{project}/{slug}")
      if feat["notes"] == notes:
          return MutationResult(project, slug, feat["status"], changed=False)
      conn.execute(
          "UPDATE features SET notes=?, updated_at=? WHERE id=?",
          (notes, now, feat["id"]),
      )
      conn.execute(
          "INSERT INTO events (document_id, event_type, payload_json, created_at) "
          "VALUES (NULL, 'feature_note_updated', ?, ?)",
          (json.dumps({"project": project, "slug": slug}), now),
      )
      return MutationResult(project, slug, feat["status"], changed=True)
  ```
  The result carries the feature's *current* status (unchanged) — the mutation never transitions status, so a `done` feature's outcome can be reworded while it stays `done`.
3. **Validation enforces the locked contract in one check**
  `body.get("notes")` returns `None` for a missing key; `not isinstance(notes, str)` then rejects both a missing key and a non-string with 400. An empty string passes the type check and is a legal value (clears the note).
  ```
  notes = body.get("notes")
  if not isinstance(notes, str):
      return JSONResponse({"error": "'notes' must be a string"}, status_code=400)
  ```
4. **Broadcast only on change**
  Fire `request.app.state.broadcaster.broadcast()` only when `result.changed`, exactly like the sibling handlers. The broadcast is a content-free refresh ping.

## Data model

No schema change. Writes the existing `features.notes` column and bumps `features.updated_at`. Records one new event type, `feature_note_updated`, in the existing `events` table with `document_id = NULL` (matching every other feature-lifecycle event). Note: feature-level events have `document_id = NULL`, so this event — like the others — does not contribute to the inbox's `last_activity` (which joins events to documents). That is intentional per the requirements: a note edit is not inbox activity.

## Contract

**Request:** `POST /api/projects/{project}/features/{feature}/note` (route param literal `{feature}`), JSON body `{"notes": "string"}`.

| Condition | Status | Body |
|---|---|---|
| Note changed | 200 | `{project, slug, status, changed: true}` |
| Note identical to stored value | 200 | `{..., changed: false}` — no event, no broadcast |
| Feature does not exist | 404 | `{"error": "feature not found"}` |
| Missing `notes` key or non-string value | 400 | `{"error": "'notes' must be a string"}` |
| Body not a JSON object | 400 | `{"error": "body must be a JSON object"}` |
| Invalid JSON | 400 | `{"error": "invalid JSON"}` |
| DB not configured | 503 | `{"error": "db not configured"}` |

Empty string is accepted and clears the note. Status is always the feature's existing status (never changed by this call).

## File structure

### Modified — implementation

- `feature_skills_webapp/storage/tracker.py` — add `update_feature_note()` beside the other mutations.
- `feature_skills_webapp/web/tracker.py` — add `note_handler()`; import `update_feature_note` from `storage.tracker`.
- `feature_skills_webapp/web/app.py` — import `note_handler`; register the `POST .../note` route next to the other tracker routes.

### Modified — tests

- `feature_skills_webapp/storage/tracker_test.py` — storage mutation tests.
- `feature_skills_webapp/web/tracker_test.py` — handler tests.

## Verification

Run from the repo root. Each command fails loudly if the endpoint is absent or misbehaving.

#### 1. Full QA suite (CI parity)

```
uv run ruff format --check . && uv run ruff check . && uv run ty check . && uv run pytest
```

#### 2. The new tests exist and pass (and pin the behaviour)

```
uv run pytest feature_skills_webapp/storage/tracker_test.py feature_skills_webapp/web/tracker_test.py \
  -k "update_feature_note or note_update" -v
```

The selector keys off the new test-name tokens (see Phase 1 test naming) so it matches only the new tests, not pre-existing `*notes*` tests.

#### 3. Live round-trip against the running service (optional belt-and-braces)

The pytest suite above is the real coverage; this curl round-trip is an extra smoke check against the live service. Restart first (code change; no dependency change): `systemctl --user restart feature-skills-webapp`. Then, using a throwaway slug:

```
# Arrange: capture a throwaway feature with an initial note
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/verify-note-tmp/capture \
  -H 'Content-Type: application/json' -d '{"notes": "initial"}'

# Act: update the note -> expect {"...","changed":true}
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/verify-note-tmp/note \
  -H 'Content-Type: application/json' -d '{"notes": "updated note"}'
# Assert it landed: the row's note is now "updated note"
curl -fsS http://127.0.0.1:8800/api/projects/feature-skills-webapp/features \
  | python3 -c "import sys,json; f=[x for x in json.load(sys.stdin)['features'] if x['slug']=='verify-note-tmp'][0]; assert f['notes']=='updated note', f; print('note updated OK')"

# Idempotent: same note again -> changed:false
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/verify-note-tmp/note \
  -H 'Content-Type: application/json' -d '{"notes": "updated note"}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['changed'] is False, d; print('idempotent OK')"

# 404 on a missing feature
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/does-not-exist-xyz/note \
  -H 'Content-Type: application/json' -d '{"notes": "x"}'   # expect 404

# 400 on non-string note
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/verify-note-tmp/note \
  -H 'Content-Type: application/json' -d '{"notes": 5}'     # expect 400

# Cleanup: drop the throwaway feature (archives it — see note below)
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/verify-note-tmp/drop
```

**Note:** `drop` sets the feature to `archived` (there is no delete endpoint), so this round-trip leaves an archived `verify-note-tmp` row plus its `feature_captured`/`feature_note_updated`/`feature_dropped` events in the real DB. Archived features are excluded from the inbox, so this is harmless, but the DB is not left pristine. Skip this section if you'd rather not leave a tombstone — the pytest suite already proves the behaviour in isolation.

## Qc

Before committing, run the full quality-control gate exactly as `CLAUDE.md` specifies (ruff format, ruff check, ty check, pytest) — all must pass. After the code change, restart the deployed service per `CLAUDE.md` (`systemctl --user restart feature-skills-webapp`; no dependency change, so no reinstall needed). Follow whatever `CLAUDE.md` says at implementation time.

## Checklist

### Phase 1: Note-update endpoint

- Add `update_feature_note()` to `storage/tracker.py` (status-preserving, idempotent, emits `feature_note_updated`).
- Add `note_handler()` to `web/tracker.py` and import `update_feature_note`.
- Register the `POST /api/projects/{project}/features/{feature}/note` route in `web/app.py`.
- Add storage mutation tests: change+event, idempotent no-op, done-preserves-status, missing-raises, fills-NULL, empty-clears.
- Add handler tests: 200 update, 404, 400 (missing/non-string/non-object), broadcast-on-change, no-broadcast-on-noop, 503.
- Confirm each new test fails without the change.
- Run the full QA gate (ruff format/check, ty, pytest) — all green.
- Run the live round-trip verification commands; restart the service first.

## Phase 1

**What's built:** the storage mutation, the handler, the route, and full test coverage — the entire in-repo deliverable in one MR.

**Files touched:** `storage/tracker.py`, `web/tracker.py`, `web/app.py`, and the two tracker test files.

#### Storage mutation

Add `update_feature_note()` (signature in Key decisions). Place it after `ship_feature`/`drop_feature` in the mutation section.

#### Handler + route

```
async def note_handler(request: Request) -> JSONResponse:
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
    notes = body.get("notes")
    if not isinstance(notes, str):
        return JSONResponse({"error": "'notes' must be a string"}, status_code=400)
    try:
        with request_conn(request.app) as conn, transaction(conn):
            result = update_feature_note(
                conn, project=project, slug=slug, notes=notes, now=now_iso()
            )
    except FeatureNotFound:
        return JSONResponse({"error": "feature not found"}, status_code=404)
    if result.changed:
        request.app.state.broadcaster.broadcast()
    return JSONResponse(
        {
            "project": result.project,
            "slug": result.slug,
            "status": result.status,
            "changed": result.changed,
        }
    )
```

Register in `web/app.py` next to the `ship`/`drop` routes:

```
Route(
    "/api/projects/{project}/features/{feature}/note",
    note_handler,
    methods=["POST"],
),
```

#### Tests

**Test naming:** name the new storage tests `test_update_feature_note_*` and the new handler tests `test_note_update_*`, so verification command #2 can select exactly the new tests (and not pre-existing `*notes*` tests).

**Storage** (`storage/tracker_test.py`, using the existing `_conn`/`_seed_project`/`_seed_feature` helpers and `transaction`):

- Changing an existing note updates it, returns `changed=True`, status unchanged, and inserts one `feature_note_updated` event.
- Re-sending the identical note returns `changed=False` and inserts no event (event-count before == after, as in `test_ship_already_done_is_noop`).
- Updating the note on a `done` feature changes the note and leaves `status == "done"`.
- Targeting a missing feature raises `FeatureNotFound`.
- Setting a note on a feature seeded with `notes=None` (stranded case) fills it in (`changed=True`).
- An empty-string note clears a previously-set note (`changed=True`, stored note == ""); re-sending `""` is then a no-op (`changed=False`) — pinning both transitions.

**Handler** (`web/tracker_test.py`, TestClient + `MagicMock` broadcaster as in `test_capture_broadcasts_on_change`):

- POST to an existing feature returns 200 with `changed=true`; the stored note is updated.
- POST to a missing feature returns 404.
- Missing `notes` key → 400; non-string `notes` → 400; non-object body → 400.
- A real change calls `broadcaster.broadcast` once; an idempotent no-op does not call it.
- 503 when the DB is unconfigured (matches the sibling 503 tests).

Confirm each new test fails without the change (per `CLAUDE.md`/the project's test discipline).

**MR:** single MR off `main`.
