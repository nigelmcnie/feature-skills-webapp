# inbox-view

## Overview

Build the webapp's Home page: a cross-project inbox that groups feature-doc cards into three categories — **New since last visit**, **In progress**, and **Recently shipped** — replacing the current placeholder at `/`. The work is a read-model layer plus a server-rendered page, with one small upstream addition: the walker emits a `shipped` event when a feature transitions to `done`, giving "Recently shipped" a real ship timestamp. No schema change. Three phases, each one MR: (1) the read model and the ship event with unit tests; (2) the `/` route and inbox template with empty / not-configured states; (3) the per-project filter and chips. Cards are display-only — click-through and the read stamp are `doc-view`'s job. The query layer mirrors `storage/read_state.py` (plain connection-taking functions), and the page is plain Starlette + Jinja2 with no HTMX (live refresh is the later `sse-refresh` feature).

## Key technical decisions

1. **Read model as plain storage functions in a new `storage/inbox.py`**
  Mirror `read_state.py`: connection-taking module functions returning display-ready rows, unit-tested against a seeded temp DB. The route stays thin. A frozen `InboxCard` dataclass is the common row shape; `build_inbox` aggregates the three category lists for the route.
  ```python
  @dataclass(frozen=True)
  class InboxCard:
      project: str
      feature: str | None        # slug; None only for non-feature rows (not emitted in v1)
      label: str                 # humanised doc type (doc cards) or "Shipped"/"In progress"
      last_activity: str | None  # ISO-8601 UTC; None if a feature has no active-doc events
      document_id: int | None = None   # set for doc cards (carried forward for doc-view)

  @dataclass(frozen=True)
  class Inbox:
      new_since: list[InboxCard]
      in_progress: list[InboxCard]
      recently_shipped: list[InboxCard]

  def build_inbox(conn: sqlite3.Connection, project: str | None = None) -> Inbox: ...
  ```
  `build_inbox` takes the project *name* (the chip value), resolves it to an id once for the first two queries, and passes the name through to `recently_shipped` (whose filter is name-based — see decision 3). **Absent name (`None`) → unfiltered inbox; a name that matches no project → short-circuit to an empty `Inbox`**. The short-circuit matters: if an unknown name resolved to `project_id = None`, the two id-based queries would skip their filter and return *everything* — so an unknown name must not fall through to `None`. (This is what makes Phase 3's `/?project=no-such → all-empty` test pass.)
  ```python
  def build_inbox(conn: sqlite3.Connection, project: str | None = None) -> Inbox:
      project_id: int | None = None
      if project is not None:
          row = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()
          if row is None:
              return Inbox([], [], [])          # unknown project name → empty, NOT unfiltered
          project_id = row["id"]
      return Inbox(
          new_since=new_since_last_visit(conn, project_id),
          in_progress=in_progress(conn, project_id),
          recently_shipped=recently_shipped(conn, project),   # name (known-good or None)
      )
  ```
  The three row→card mappers are small and explicit (a doc card humanises its type; the feature/ship cards carry a fixed label and no `document_id`):
  ```python
  def _doc_card(r: sqlite3.Row) -> InboxCard:
      return InboxCard(project=r["project"], feature=r["feature"],
                       label=humanise_type(r["doc_type"]), last_activity=r["last_activity"],
                       document_id=r["document_id"])

  def _feature_card(r: sqlite3.Row, *, label: str) -> InboxCard:
      return InboxCard(project=r["project"], feature=r["feature"],
                       label=label, last_activity=r["last_activity"])

  def _shipped_card(r: sqlite3.Row) -> InboxCard:
      return InboxCard(project=r["project"], feature=r["slug"],
                       label="Shipped", last_activity=r["shipped_at"])
  ```
  Module imports for `inbox.py`: `from __future__ import annotations`, `import sqlite3`, and `from datetime import UTC, datetime, timedelta` (used by `recently_shipped`).
2. **"New since last visit" reuses the unread predicate, restricted to feature-owned active docs**
  Build on the existing `unread_document_ids` comparison rather than re-deriving it, but select display columns and the document's latest event time, and **INNER JOIN `features`** so null-`feature_id` docs — notably each project's `features.html` tracker (`type='features'`) — are dropped. `status='active'` already excludes `missing`/`archived`. No cap; ordered by latest event desc.
  ```python
  def new_since_last_visit(conn, project_id: int | None = None) -> list[InboxCard]:
      sql = (
          "SELECT d.id AS document_id, d.type AS doc_type, p.name AS project, "
          "  f.slug AS feature, "
          "  (SELECT MAX(e.created_at) FROM events e WHERE e.document_id = d.id) AS last_activity "
          "FROM documents d "
          "JOIN projects p ON d.project_id = p.id "
          "JOIN features  f ON d.feature_id = f.id "          # INNER JOIN drops tracker / null-feature docs
          "WHERE d.status = 'active' AND EXISTS ("
          "  SELECT 1 FROM events e WHERE e.document_id = d.id "
          "  AND e.created_at > COALESCE("
          "    (SELECT last_read_at FROM read_state WHERE document_id = d.id), ''))"
      )
      params: list[object] = []
      if project_id is not None:
          sql += " AND d.project_id = ?"   # noqa: S608  (interpolation is a fixed clause, params bound)
          params.append(project_id)
      sql += " ORDER BY last_activity DESC"
      return [_doc_card(r) for r in conn.execute(sql, params).fetchall()]
  ```
3. **"Recently shipped" derives from a `shipped` event, not `features.updated_at`**
  The walker bumps `updated_at` on every tracker upsert, so it is not a ship time (requirements Design notes). Instead the walker emits a `shipped` event on the done-transition. The `events` table has no `feature_id` (only a nullable `document_id`), and the design doc's §6 `sse-refresh` already anticipates a "ship" event type — so the event is stored with `document_id = NULL` and the feature carried in `payload_json` as `{"project": <name>, "slug": <slug>}`, consistent with the walker's existing slug/path-in-payload style. The query reads shipped events inside the recency window, keeps the latest per feature, caps to N, ordered newest first.
  ```python
  SHIPPED_RECENT_DAYS = 30
  SHIPPED_LIMIT = 5

  def recently_shipped(conn, project: str | None = None, *,
                       limit: int = SHIPPED_LIMIT,
                       within_days: int = SHIPPED_RECENT_DAYS) -> list[InboxCard]:
      cutoff = (datetime.now(tz=UTC) - timedelta(days=within_days)).isoformat()
      sql = (
          "SELECT json_extract(payload_json,'$.project') AS project, "
          "  json_extract(payload_json,'$.slug') AS slug, "
          "  MAX(created_at) AS shipped_at "
          "FROM events WHERE event_type = 'shipped' AND created_at > ?"
      )
      params: list[object] = [cutoff]
      if project is not None:
          sql += " AND json_extract(payload_json,'$.project') = ?"   # noqa: S608
          params.append(project)
      sql += " GROUP BY project, slug ORDER BY shipped_at DESC LIMIT ?"
      params.append(limit)
      return [_shipped_card(r) for r in conn.execute(sql, params).fetchall()]
  ```
  The cutoff must be produced exactly as `now_iso()` produces stored timestamps — `datetime.now(tz=UTC).isoformat()`, minus the `timedelta` — so both sides of the `>` are the same `+00:00` ISO-8601 shape and the comparison is the same lexicographic one `read_state` relies on. (Don't hand-format or use a different offset spelling; `isoformat()` drops the microseconds component when it's exactly zero, and only matching shapes compare safely.) Add a boundary test: a ship event whose `created_at` sits just inside vs just outside the window lands on the expected side — mirroring `read_state`'s `test_equal_timestamp_tie_reads_as_read` discipline. Existing `done` features have no shipped event (no backfill) and simply don't appear — they age out; acceptable per requirements.
4. **Walker emits the ship event by diffing status in `_apply_tracker_rows`**
  `_apply_tracker_rows` currently blind-upserts. Read the existing status first; after the upsert, if the new status is `done` and the old was not, insert the event. The function gains the project name (the caller already has `identity.project`). Re-running the walk on an already-`done` feature is a no-op (old status is `done`), so no duplicate events.
  ```python
  def _apply_tracker_rows(conn, project_id: int, project_name: str,
                          rows: list[TrackerRow], now: str) -> None:
      for row in rows:
          prev = conn.execute(
              "SELECT status FROM features WHERE project_id=? AND slug=?",
              (project_id, row.slug),
          ).fetchone()
          old_status = prev["status"] if prev else None
          conn.execute(
              "INSERT INTO features (project_id, slug, status, owner, notes, created_at, updated_at) "
              "VALUES (?, ?, ?, ?, ?, ?, ?) "
              "ON CONFLICT(project_id, slug) DO UPDATE SET "
              "status=excluded.status, owner=excluded.owner, notes=excluded.notes, "
              "updated_at=excluded.updated_at",
              (project_id, row.slug, row.status, row.owner, row.notes, now, now),
          )
          if row.status == "done" and old_status != "done":
              conn.execute(
                  "INSERT INTO events (document_id, event_type, payload_json, created_at) "
                  "VALUES (NULL, 'shipped', ?, ?)",
                  (json.dumps({"project": project_name, "slug": row.slug}), now),
              )
  # caller (in _process_file):
  #   _apply_tracker_rows(conn, project_id, identity.project, parse_tracker(html_content), now)
  ```
  Two pre-existing facts to keep in mind: the call runs inside the single walk transaction (`walk()` wraps `_process_file` in `with transaction(conn)`), so the ship-event insert commits atomically with the walk; and `_process_file` wraps `_apply_tracker_rows` in a `try/except Exception` that logs and swallows (so a tracker mishap can't abort the walk) — the new SELECT/INSERT now runs under that same guard, which is fine but means a failure here is silent.
5. **The `/` route renders the inbox; degrades when unconfigured**
  Replace the placeholder `index`. When `db_path is None`, render a "not configured / no data yet" state (keeps `test_index_returns_200` green with `db_path=None`). Otherwise open a per-request connection, build the inbox, and render. A category with no rows is omitted by the template; when all three are empty, render the single "Nothing's waiting for you" state. The `MARKER` constant and its placeholder assertion are removed.
  ```python
  async def index(request: Request) -> HTMLResponse:
      app = request.app
      templates: Jinja2Templates = app.state.templates
      if app.state.db_path is None:
          return templates.TemplateResponse(request, "index.html", {"configured": False})
      from feature_skills_webapp.storage.inbox import build_inbox
      from feature_skills_webapp.web.db_dep import request_conn
      project = request.query_params.get("project")            # Phase 3
      with request_conn(app) as conn:
          inbox = build_inbox(conn, project=project)
          projects = [r["name"] for r in                        # Phase 3 (chips)
                      conn.execute("SELECT name FROM projects ORDER BY name").fetchall()]
      return templates.TemplateResponse(request, "index.html", {
          "configured": True, "inbox": inbox,
          "projects": projects, "active_project": project,
      })
  ```
6. **Humanised type labels via a small mapping**
  Cards show a readable label, not the raw `documents.type`. A dict covers the common types; unknowns fall back to a title-cased, hyphen-spaced form (so a future `requirements-feedback-1` reads as "Requirements feedback 1").
  ```python
  _TYPE_LABELS = {"context": "Context", "requirements": "Requirements",
                  "plan": "Plan", "review": "Review", "features": "Tracker"}

  def humanise_type(doc_type: str) -> str:
      if doc_type in _TYPE_LABELS:
          return _TYPE_LABELS[doc_type]
      return doc_type.replace("-", " ").replace("_", " ").capitalize()
  ```

## File structure

### New files

- `feature_skills_webapp/storage/inbox.py` — read model: `InboxCard`, `Inbox`, `humanise_type`, the three category functions, `build_inbox`.
- `feature_skills_webapp/storage/inbox_test.py` — unit tests for the read model against a seeded temp DB.

### Modified files

- `feature_skills_webapp/storage/walker.py` — `_apply_tracker_rows` emits the `shipped` event (Phase 1).
- `feature_skills_webapp/storage/walker_test.py` — ship-event tests (Phase 1).
- `feature_skills_webapp/web/routes.py` — `index` renders the inbox; drop `MARKER` (Phase 2); read `?project` (Phase 3).
- `feature_skills_webapp/web/templates/index.html` — inbox markup: not-configured / empty / populated states, category sections + cards (Phase 2); chips (Phase 3).
- `feature_skills_webapp/web/routes_test.py` — replace the `MARKER` test with inbox/empty/not-configured assertions (Phase 2); filter tests (Phase 3).

## Phase 1 — Read model + ship event

### What's built

The data layer: `storage/inbox.py` (decisions 1, 2, 3, 6) and the walker's `shipped` event (decision 4). No UI. `in_progress` selects `features.status='in_progress'` joined to `projects`, last activity = most recent event over the feature's *active* docs, ordered by that desc (use `COALESCE(last_activity,'')` so featureless-of-events rows sort last deterministically):

```python
def in_progress(conn, project_id: int | None = None) -> list[InboxCard]:
    sql = (
        "SELECT p.name AS project, f.slug AS feature, "
        "  (SELECT MAX(e.created_at) FROM events e "
        "   JOIN documents d ON e.document_id = d.id "
        "   WHERE d.feature_id = f.id AND d.status='active') AS last_activity "
        "FROM features f JOIN projects p ON f.project_id = p.id "
        "WHERE f.status = 'in_progress'"
    )
    params: list[object] = []
    if project_id is not None:
        sql += " AND f.project_id = ?"   # noqa: S608
        params.append(project_id)
    sql += " ORDER BY COALESCE(last_activity,'') DESC"
    return [_feature_card(r, label="In progress") for r in conn.execute(sql, params).fetchall()]
```

### Files touched

New `storage/inbox.py`, `storage/inbox_test.py`; modified `storage/walker.py`, `storage/walker_test.py`.

### Tests

- **inbox_test.py** — seed two projects, features in mixed statuses, docs (active/archived/missing, plus a null-feature `features` doc), events at OLD/FUTURE timestamps, and read_state rows, following `read_state_test._seed`'s style.
- `new_since_last_visit`: returns unread active feature docs; excludes archived, missing, read, and the null-feature tracker doc; ordered newest-first; project filter narrows correctly.
- `in_progress`: returns only `in_progress` features; ordered by most-recent active-doc event; a feature with no active-doc events still appears (sorts last); project filter works.
- `recently_shipped`: returns features with a `shipped` event inside the window, newest first, capped at `limit`; excludes events older than the cutoff; keeps only the latest event per feature; project filter (by name) works; a **cutoff-boundary** case (event just inside vs just outside the window) lands on the expected side.
- `build_inbox`: `None` → unfiltered; a known name → that project's rows only; an **unknown name → empty `Inbox`** (not unfiltered).
- `humanise_type`: known types map; unknown falls back to title-cased spaced form.
- **walker_test.py** — a tracker re-walk where a feature flips `available/in_progress → done` inserts exactly one `shipped` event with the right `{project, slug}` payload and `document_id IS NULL`; a feature already `done` across two walks produces no duplicate; a non-done status produces none.

### MR chain

One MR titled `feat(inbox-view): phase 1 — read model + ship event`.

## Phase 2 — The Home page

### What's built

The `/` route (decision 5) and a rewritten `templates/index.html` rendering the inbox cross-project. The template branches: `{% if not configured %}` → "no data yet" panel; `{% elif inbox is all-empty %}` → "Nothing's waiting for you" with a pointer to project views; `{% else %}` → up to three `<section>`s ("New since last visit", "In progress", "Recently shipped"), each rendered only when its list is non-empty, as a list of cards. A card shows project · feature · label · a relative/ISO last-activity. Reuse the dark palette (the `--bg/--surface/--accent` variables) inline in the template, consistent with the existing `index.html` styling. Cards are display-only — no link target. `MARKER` is removed from `routes.py`.

### Files touched

Modified `web/routes.py`, `web/templates/index.html`, `web/routes_test.py`.

### Tests

- Replace `test_index_contains_marker` and remove the now-dangling `from feature_skills_webapp.web.routes import MARKER` import at the top of `routes_test.py`: with `db_path=None`, `/` returns 200 and renders the not-configured state (assert a stable marker string from that branch).
- With a seeded `temp_db` (insert projects/features/docs/events/read_state directly, or run `/admin/discover` over a built docs-root like `make_docs_root`), `/` renders cards: an unread doc's feature/label appears; an `in_progress` feature appears; a feature with a recent `shipped` event appears under "Recently shipped".
- Empty DB (migrated, no rows): `/` returns 200 and shows the "Nothing's waiting for you" state, not a category heading.
- Keep `test_index_returns_200` and `test_index_still_ok_with_new_lifespan` green.

### MR chain

One MR titled `feat(inbox-view): phase 2 — inbox home page`.

## Phase 3 — Per-project filter

### What's built

The `?project=<name>` filter end-to-end: the route already reads the param and passes it to `build_inbox` (decision 5); add the chip row to the template — one chip per project plus an "All" chip, the active one highlighted, each linking to `/?project=<name>` (and `/` for All). An unknown project name produces an empty inbox (every category filtered to a project that matches nothing) — render the all-empty state. Server-rendered links, no JS.

### Files touched

Modified `web/templates/index.html` (chips) and `web/routes_test.py` (filter tests). Route wiring already present from Phase 2.

### Tests

- Seed two projects with distinct unread docs; `/?project=proj-a` renders proj-a's cards and not proj-b's; `/` renders both.
- Chips: the rendered page lists a chip per project and marks the active one.
- `/?project=no-such` returns 200 and renders the all-empty state.

### MR chain

One MR titled `feat(inbox-view): phase 3 — per-project filter`.

## QC

This repo has no `CLAUDE.md`; QC is the README's "Development" section. Before each phase commit, run from the repo root:

```bash
uv run pytest
uv run ruff format .
uv run ruff check .
uv run ty check .
```

All must pass clean. Match existing conventions: `from __future__ import annotations` at module top; type hints on every function; SQL built with string concatenation that interpolates only fixed clauses (values always bound as params) carries a `# noqa: S608`, as in `read_state.py` and `walker.py`. British English in comments/docs. Per the global staging rule: `git status` first, then `git add` only the named files for that phase — never `git add -A`.

## Checklist

### Phase 1: Read model + ship event

- Create `storage/inbox.py` with `InboxCard`, `Inbox`, and the `humanise_type` helper + `_TYPE_LABELS` (decision 6).
- Implement `new_since_last_visit(conn, project_id=None)` — INNER JOIN features, active-only, EXISTS unread predicate, ordered by latest event desc, optional project filter (decision 2).
- Implement `in_progress(conn, project_id=None)` — `status='in_progress'`, last activity over active docs, `COALESCE`-ordered desc, optional project filter.
- Implement `recently_shipped(conn, project=None, *, limit, within_days)` — shipped events in window, latest-per-feature, capped, newest first, optional name filter (decision 3).
- Implement `build_inbox(conn, project=None)` and the `_doc_card`/`_feature_card`/`_shipped_card` mappers — resolve name→id once, **unknown name short-circuits to an empty `Inbox`** (not unfiltered), call the three functions, return `Inbox` (decision 1).
- Modify `_apply_tracker_rows` to take the project name, diff old status, and emit a `shipped` event on the done-transition; update its caller in `_process_file` (decision 4).
- Write `storage/inbox_test.py` covering all three category functions (inclusion, exclusions, ordering, project filter) and `humanise_type`.
- Add walker tests: ship event on transition (payload + null document_id), no duplicate on repeat walk, none for non-done.
- Run QC (pytest, ruff format, ruff check, ty check); commit named files as `feat(inbox-view): phase 1 — read model + ship event`; push.

### Phase 2: The Home page

- Rewrite `index` in `routes.py` to gate on `db_path`, build the inbox, and pass it to the template; remove `MARKER` (decision 5).
- Rewrite `templates/index.html` as the inbox: not-configured panel, all-empty state, and up to three category sections of cards (project · feature · label · last activity), reusing the dark palette.
- Update `routes_test.py`: remove the `MARKER` import, replace the marker test with not-configured-state, populated-inbox, and empty-state assertions; keep the existing 200/lifespan tests green.
- Run QC; commit named files as `feat(inbox-view): phase 2 — inbox home page`; push.

### Phase 3: Per-project filter

- Add the chip row to `index.html` — one chip per project plus "All", active chip highlighted, linking to `/?project=<name>`.
- Confirm the route passes `?project` through and supplies the `projects` list (already wired in Phase 2); handle unknown-name → all-empty.
- Add `routes_test.py` filter tests: project-scoped render, chip presence/active marking, unknown-project empty state.
- Run QC; commit named files as `feat(inbox-view): phase 3 — per-project filter`; push.
