# read-state

## Overview

Add a `storage/read_state.py` module with three operations — mark one document read, mark every active document in a project read, and query which active documents are unread (have an event newer than their `last_read_at`) — backed by the existing `read_state` table (no migration). A shared `now_iso()` helper is extracted into `storage/db.py` and adopted by both the walker and read-state, so `last_read_at` and `events.created_at` are guaranteed to share the exact textual format the unread comparison depends on. Phase 1 ships the storage layer (unblocking `inbox-view`); Phase 2 adds the bulk "mark all read" admin endpoint. The per-render stamp is wired in later by `doc-view`, which will call `mark_read()` server-side.

## Key technical decisions

1. **Single shared timestamp helper in `storage/db.py`**
  The unread test is a lexicographic string comparison, valid only if `last_read_at` and `events.created_at` share an identical format (UTC, ISO-8601, microsecond precision, `+00:00` offset). To prevent drift, extract one helper and have both the walker and read-state call it. `storage/db.py` is the shared home (the walker already imports `transaction` from it).
  ```python
  # storage/db.py — new
  from datetime import UTC, datetime

  def now_iso() -> str:
      """UTC ISO-8601 timestamp: the single source of truth for stored timestamps.

      read_state.last_read_at and events.created_at are compared
      lexicographically, so they MUST be produced by this one function.
      """
      return datetime.now(tz=UTC).isoformat()
  ```
  In `storage/walker.py`, replace the inlined `now = datetime.now(tz=UTC).isoformat()` in `walk()` with `now = now_iso()` (import it from `.db`). The `datetime`/`UTC` import stays — it's still used for `source_mtime`. Existing walker tests should pass unchanged; this is a pure refactor of how the timestamp string is produced.
  **Precision note.** `datetime.isoformat()` omits the fractional part when `microsecond == 0` (e.g. `2026-01-01T00:00:00+00:00`). This does *not* break the lexicographic comparison — `+` (0x2B) sorts before `.` (0x2E), so an event at `…SS.000005…` still reads as newer than a read at `…SS+00:00`. The only consequence is that tests must not assert the *presence* of fractional digits (see Phase 1 tests). If a fixed width is ever wanted, it goes in this one helper — the shared-helper design keeps that change in a single place.
2. **Unread predicate: active doc with an event newer than last-read**
  A correlated `EXISTS` over `events`, with `COALESCE(last_read_at, '')` giving the "missing row = never read" behaviour for free (`''` sorts before any ISO timestamp). Document-level `status='active'` filtering — not event-type filtering — keeps archived/missing docs out. An optional `project_id` serves both per-project and cross-project callers. Returns bare document ids; `inbox-view` joins those to whatever columns it renders.
  ```python
  def unread_document_ids(
      conn: sqlite3.Connection, project_id: int | None = None
  ) -> list[int]:
      sql = (
          "SELECT d.id FROM documents d "
          "WHERE d.status = 'active' "
          "AND EXISTS ("
          "  SELECT 1 FROM events e "
          "  WHERE e.document_id = d.id "
          "  AND e.created_at > COALESCE("
          "    (SELECT last_read_at FROM read_state WHERE document_id = d.id), ''"
          "  )"
          ")"
      )
      params: list[object] = []
      if project_id is not None:
          sql += " AND d.project_id = ?"
          params.append(project_id)
      return [row["id"] for row in conn.execute(sql, params).fetchall()]
  ```
  The comparison is strict (`>`), so an event exactly at `last_read_at` reads as already-read — the deliberate tie-break from the requirements.
  **Lint note.** The project filter is concatenated onto the SQL string, so `ruff` will flag `S608` (dynamic SQL). The fragment is a constant, not user input — a false positive. Add `# noqa: S608` on the concatenation line, as the walker's reconcile query already does, or QC's `ruff check` fails on the first run.
3. **Idempotent upsert through the existing `transaction()` helper**
  `mark_read` upserts one row keyed on the `document_id` PK; calling it repeatedly (as a render path will) just rewrites `last_read_at`. All writes go through `transaction()` (BEGIN IMMEDIATE), never bare `with conn:`, per the storage conventions.
  ```python
  def mark_read(conn: sqlite3.Connection, document_id: int) -> None:
      with transaction(conn):
          conn.execute(
              "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?) "
              "ON CONFLICT(document_id) DO UPDATE SET last_read_at = excluded.last_read_at",
              (document_id, now_iso()),
          )
  ```
4. **Bulk endpoint: `POST /admin/projects/{project}/mark-read`, name-keyed**
  The project is identified by name (matches the design's name-keyed routes; `projects.name` is `UNIQUE`). The handler resolves name → id, calls `mark_all_read`, and returns a JSON summary in the style of `admin_discover`. Unknown project → 404; unconfigured DB (`db_path is None`) → 503. (This is a DB-specific guard, distinct from `admin_discover`'s `walk_queue` check — mark-read needs the DB, not the walk queue. `create_app` always sets `app.state.db_path`, so reading it is safe.) `mark_all_read` stamps every *active* doc in the project and returns the count stamped.
  ```python
  def mark_all_read(conn: sqlite3.Connection, project_id: int) -> int:
      now = now_iso()
      with transaction(conn):
          rows = conn.execute(
              "SELECT id FROM documents WHERE project_id = ? AND status = 'active'",
              (project_id,),
          ).fetchall()
          for r in rows:
              conn.execute(
                  "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?) "
                  "ON CONFLICT(document_id) DO UPDATE SET last_read_at = excluded.last_read_at",
                  (r["id"], now),
              )
      return len(rows)
  ```
  ```python
  # web/routes.py — new handler
  async def admin_mark_read(request: Request) -> JSONResponse:
      if request.app.state.db_path is None:
          return JSONResponse({"error": "db not configured"}, status_code=503)
      project = request.path_params["project"]
      from feature_skills_webapp.storage.read_state import mark_all_read
      from feature_skills_webapp.web.db_dep import request_conn

      with request_conn(request.app) as conn:
          row = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()
          if row is None:
              return JSONResponse({"error": "unknown project"}, status_code=404)
          stamped = mark_all_read(conn, row["id"])
      return JSONResponse({"project": project, "stamped": stamped})
  ```

## File structure

### New files

- `feature_skills_webapp/storage/read_state.py` — `mark_read`, `mark_all_read`, `unread_document_ids`.
- `feature_skills_webapp/storage/read_state_test.py` — unit tests for all three ops + the unread predicate.

### Modified files

- `feature_skills_webapp/storage/db.py` — add `now_iso()`.
- `feature_skills_webapp/storage/walker.py` — use `now_iso()` in `walk()`.
- `feature_skills_webapp/web/routes.py` — add `admin_mark_read` handler.
- `feature_skills_webapp/web/app.py` — register the `POST /admin/projects/{project}/mark-read` route.
- `feature_skills_webapp/web/routes_test.py` — endpoint tests (200 / 404 / 503).

## Phase 1 — Storage ops + unread query

### What's built

The `now_iso()` helper (and the walker's adoption of it), plus `read_state.py` with `mark_read` and `unread_document_ids`. No HTTP surface. This is the phase that unblocks `inbox-view`.

### Files touched

`storage/db.py`, `storage/walker.py`, `storage/read_state.py` (new), `storage/read_state_test.py` (new).

### Tests

In `read_state_test.py`, against a temp DB (`connect` + `migrate`, mirroring `walker_test.py`'s `temp_conn`). Seed `projects`/`documents`/`events` rows directly; use **fixed** timestamps for seeded events (e.g. `'2020-01-01T00:00:00+00:00'` for "old", `'2099-01-01T00:00:00+00:00'` for "newer than any read") so comparisons are deterministic rather than racing the wall clock.

- `now_iso()` round-trips via `datetime.fromisoformat` with `tzinfo == UTC`, and two successive calls are non-decreasing lexicographically. (Do *not* assert the presence of fractional/microsecond digits — they're absent at whole-microsecond instants; see the precision note in decision 1.)
- A doc with an (old) event and no `read_state` row is reported unread.
- After `mark_read`, that doc is no longer unread.
- A subsequent event with a future timestamp flips it back to unread.
- `mark_read` is idempotent — calling it twice leaves exactly one `read_state` row and advances `last_read_at`.
- `archived` and `missing` docs (each with a fresh event) are excluded from the unread set.
- Project filter: with unread docs in two projects, `unread_document_ids(project_id=A)` returns only A's; `unread_document_ids()` returns both.

### MR chain

One MR titled `feat(read-state): phase 1 — storage ops + unread query`.

## Phase 2 — Bulk mark-all-read endpoint

### What's built

`mark_all_read` in `read_state.py`, the `admin_mark_read` handler in `routes.py`, and the route registration in `app.py`.

### Files touched

`storage/read_state.py`, `storage/read_state_test.py`, `web/routes.py`, `web/app.py`, `web/routes_test.py`.

### Tests

**Storage** (`read_state_test.py`): `mark_all_read` stamps every active doc in the target project and returns that count; it leaves `archived`/`missing` docs and other projects untouched (assert via `unread_document_ids` before/after).

**Endpoint** (`routes_test.py`, using `TestClient` with `create_app(db_path=temp_db, docs_root=…)` as `test_admin_discover_returns_summary` does — the lifespan's reconcile walk populates docs + events):

- `POST /admin/projects/{name}/mark-read` returns 200 with `{"project", "stamped"}`; `stamped` equals the project's active-doc count; afterwards a direct `unread_document_ids(project_id)` on the temp DB is empty.
- An unknown project name returns 404.
- `create_app(db_path=None)` → the endpoint returns 503 (DB not configured).

### MR chain

One MR titled `feat(read-state): phase 2 — bulk mark-all-read endpoint`.

## QC

This repo has no `CLAUDE.md`; QC steps come from the README's "Development" section. Before each commit, run and pass:

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run ty check .
```

Tests run under `pytest-socket` (sockets disabled) with `xdist` and a per-worker DB — keep everything offline and temp-DB-scoped. Follow whatever the README says at implementation time if it has changed.

## Checklist

### Phase 1: Storage ops + unread query

- Add `now_iso()` to `storage/db.py`.
- Refactor `walk()` in `storage/walker.py` to use `now_iso()`.
- Create `storage/read_state.py` with `mark_read` and `unread_document_ids`.
- Write `storage/read_state_test.py`: now_iso format, no-row=unread, mark-read clears, later-event re-flags, idempotency, archived/missing excluded, project filter.
- Run QC (pytest + ruff format/check + ty); confirm existing walker tests still pass.
- Commit and open MR `feat(read-state): phase 1 — storage ops + unread query`.

### Phase 2: Bulk mark-all-read endpoint

- Add `mark_all_read` to `storage/read_state.py`.
- Add the `admin_mark_read` handler to `web/routes.py` (404 unknown project, 503 unconfigured DB).
- Register `POST /admin/projects/{project}/mark-read` in `web/app.py` (add `admin_mark_read` to the `from …web.routes import` line and a `Route(…, methods=["POST"])` entry).
- Add `mark_all_read` storage tests (stamps active only; ignores other projects + archived/missing).
- Add endpoint tests to `web/routes_test.py`: 200 + summary + unread cleared, 404 unknown project, 503 unconfigured DB.
- Run QC (pytest + ruff format/check + ty).
- Commit and open MR `feat(read-state): phase 2 — bulk mark-all-read endpoint`.
