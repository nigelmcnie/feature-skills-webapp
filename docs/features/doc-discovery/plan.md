# doc-discovery

## Overview

Build the walker that indexes the dev-store into the (currently empty) SQLite schema the skeleton laid. A synchronous `walk()` in a new `storage/walker.py` recurses the docs-root, derives project/feature/type/status from each `.html` file's path and `feature-doc-type` meta tag, and upserts `projects`/`features`/`documents` (mtime+size gated) while appending an `events` row per change. All triggers feed a single serialised walk task so the background writer never contends with itself. Delivered in three independently testable MRs: **Phase 1** — walker core + migration 0002 + startup & on-demand triggers; **Phase 2** — a `watchfiles` watch for live freshness (brought in early to surface watch edge cases); **Phase 3** — parsing `features.html` to populate `features.status`/`owner`/`notes`.

## Key technical decisions

1. **Migration 0002 drops and recreates `documents` (it's provably empty)**
  Making `feature_id` nullable needs a new table shape, and the canonical SQLite rebuild's `PRAGMA foreign_keys=OFF` can't run inside `migrate()`'s single `BEGIN IMMEDIATE`, so we `DROP` and re-`CREATE` instead. **Why it's safe:** `DROP TABLE documents` succeeds regardless of the child FKs — SQLite does not enforce foreign keys on `DROP TABLE` the way it does on row `DELETE`. What guarantees correctness is that `documents` is empty when 0002 applies (the walker is its first and only writer, shipped with 0002): no document rows are lost, and no orphaned child rows are left behind. (`read_state` cascades, but `comments`/`synthesis_responses`/`events` would *not* — so this DROP+CREATE pattern is only safe because the table is empty; do not reuse it on a populated table.) The new shape adds `project_id` (a project-level doc has no feature, so it can't reach its project via `features` — see decision 2), a `status` column, and a partial unique index on `source_path`. Each migration file inserts its own version row (the runner doesn't), mirroring `0001`.
  ```sql
  -- storage/migrations/0002_documents_status.sql
  DROP TABLE documents;

  CREATE TABLE documents (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      feature_id INTEGER REFERENCES features(id) ON DELETE CASCADE,   -- now nullable
      type TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active',                          -- active | archived | missing
      source_path TEXT,
      content_html TEXT,
      metadata_json TEXT,
      source_mtime TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
  );

  CREATE INDEX idx_documents_project ON documents(project_id);
  CREATE INDEX idx_documents_feature ON documents(feature_id);
  CREATE UNIQUE INDEX idx_documents_source_path
      ON documents(source_path) WHERE source_path IS NOT NULL;

  INSERT INTO schema_version (version) VALUES (2);
  ```
  All plain DDL, so the runner's naive `;`-split is safe — but terminate every statement with `;` (don't copy `0001`'s terminator-less final line mid-file, or two statements glue together). (If rows ever needed preserving across such a change, use `PRAGMA defer_foreign_keys=ON` + `INSERT … SELECT` instead — not needed here.) A full drop-all-and-rebuild "nuclear" reset is deliberately out of scope for Stage 1 (reconcile, or deleting the regenerable DB file, suffices); add a rebuild endpoint later only if needed.
2. **Identity from path; the walker owns the whole spine**
  A pure derivation maps a file path (relative to docs-root) to its identity. The walker upserts `projects` by name and `features` by `(project_id, slug)` — both already carry UNIQUE constraints — then the `documents` row. `project_id` is always set; `feature_id` is set for per-feature docs and NULL for project-level ones (`features.html`, Phase 3).
  ```python
  # storage/walker.py
  @dataclass(frozen=True)
  class DocIdentity:
      project: str
      feature: str | None     # None for project-level docs (features.html)
      archived: bool          # True if under a .feedback-archive/ dir

  def identity_for(rel_path: Path) -> DocIdentity | None:
      """Map a docs-root-relative .html path to its identity, or None to skip."""
      parts = rel_path.parts            # excludes docs_root itself
      # exactly these shapes are indexed; everything else -> None (skip + log):
      #   (project, "features.html")                              -> project-level (P3)
      #   (project, feature, "<doc>.html")                       -> feature doc, active
      #   (project, feature, ".feedback-archive", "<doc>.html")   -> feature doc, archived
      # skipped: bare files at docs-root depth (len 1), any .html deeper than the
      # archive shape, and any segment starting with "." other than the single
      # ".feedback-archive" component in the depth-4 form.
  ```
  Type comes from the `feature-doc-type` meta tag, not the filename; `status` is `archived` when the path is under `.feedback-archive/`, else `active`.
3. **A synchronous `walk()` holding all index logic; mtime+size gated**
  The walker is plain synchronous code (testable with a temp dir + temp DB, no event loop). Two modes: an *incremental* walk upserts what it finds; a *reconcile* additionally marks unseen rows `missing`. Gating stores the file's size alongside its mtime in `metadata_json` and re-reads only when either moved. A no-op walk writes nothing (and emits no events).
  ```python
  @dataclass
  class WalkSummary:
      created: int = 0
      updated: int = 0
      archived: int = 0
      missing: int = 0
      reactivated: int = 0
      errors: int = 0
      duration_ms: int = 0

  def walk(conn: sqlite3.Connection, docs_root: Path, *, reconcile: bool) -> WalkSummary:
      """Index every *.html under docs_root. With reconcile=True, mark rows whose
      source_path was not seen this pass as status='missing'. All writes run inside
      one transaction() per the existing storage conventions."""
  ```
  **Upsert + status transitions** (keyed on `source_path`, which the partial unique index guarantees unique). **Stat before parse** — only read/parse the file on a miss or a mtime/size change, so an unchanged store is a pure `stat()` sweep:
  ```python
  # pseudo-logic inside walk(), per file
  st = path.stat()                  # cheap; do NOT read the file yet
  row = conn.execute("SELECT id, status, source_mtime, metadata_json FROM documents "
                     "WHERE source_path=?", (str(path),)).fetchone()
  mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
  if row and row.status != 'missing' \
         and row.source_mtime == mtime \
         and json.loads(row.metadata_json or '{}').get('size') == st.st_size:
      return                        # gated out: no read, no write, no event
  parsed = parse_doc(path)          # read now; None -> skip + log (e.g. mid-write)
  if parsed is None:
      return
  meta = {"title": parsed.title, "size": st.st_size}   # metadata_json shape
  desired = 'archived' if archived else 'active'
  if row is None:
      insert document (status=desired, source_mtime=mtime, metadata_json=meta); emit 'created'
  else:
      update row (status=desired, source_mtime=mtime, metadata_json=meta); emit one of:
          'reactivated'  if row.status == 'missing'
          'archived'     if desired == 'archived' and row.status != 'archived'
          'updated'      otherwise
  ```
  `source_mtime` is stored as ISO-8601 UTC (TEXT, matching the kea timestamp convention); the file **size** lives in `metadata_json` alongside the title and is the precise second gate. Reconcile's mark-missing step updates any `active`/`archived` row whose `source_path` wasn't seen to `status='missing'` and emits a `missing` event — never a hard delete (protects child read-state/comments that cascade off `documents`). A returning file reactivates its row in place rather than inserting a duplicate.
4. **Meta + title via the stdlib; no new parsing dependency**
  `parse_doc()` uses `html.parser.HTMLParser` to pull the `feature-doc-type` meta tag and `<title>`; the title is stored in `metadata_json` (with the size). No meta tag → return `None` (skip + log; covers a file caught mid-write). `content_html` stays NULL in Stage 1 — the files remain canonical.
  ```python
  @dataclass(frozen=True)
  class ParsedDoc:
      doc_type: str        # from <meta name="feature-doc-type" content="...">
      title: str | None    # from <title>

  def parse_doc(path: Path) -> ParsedDoc | None: ...
  ```
5. **One serialised walk task; the endpoint awaits its own walk**
  doc-discovery is the webapp's first long-lived background writer. To keep a single SQLite writer, all triggers post to one `asyncio.Queue`; one worker coroutine drains it, coalesces the batch, and runs *one* walk in a worker thread (`asyncio.to_thread`, its own connection — connections aren't shareable across threads). Each request carries an optional future the worker resolves with the summary, so the on-demand endpoint receives the summary of a walk that *observed its request* (it's only picked up after enqueue), not a stale one.
  ```python
  # web/discovery.py
  @dataclass
  class WalkRequest:
      reconcile: bool
      future: asyncio.Future | None   # set by the worker with the WalkSummary

  def _run_walk(db_path: Path, docs_root: Path, reconcile: bool) -> WalkSummary:
      """Runs in a worker thread: its OWN connection (sqlite conns aren't
      shareable across threads), closed in finally."""
      conn = connect(db_path)
      try:
          return walk(conn, docs_root, reconcile=reconcile)
      finally:
          conn.close()

  async def _worker(app) -> None:
      q: asyncio.Queue[WalkRequest] = app.state.walk_queue
      batch: list[WalkRequest] = []
      try:
          while True:
              batch = [await q.get()]
              while not q.empty():
                  batch.append(q.get_nowait())
              reconcile = any(r.reconcile for r in batch)
              try:
                  summary = await asyncio.to_thread(_run_walk, app.state.db_path,
                                                    app.state.docs_root, reconcile)
              except Exception:                  # walk errors are logged, never fatal
                  log.exception("walk failed"); summary = WalkSummary(errors=1)
              for r in batch:
                  if r.future and not r.future.done():
                      r.future.set_result(summary)
              batch = []
      except asyncio.CancelledError:
          # shutdown: never leave an awaiting endpoint hanging
          for r in batch:
              if r.future and not r.future.done():
                  r.future.set_result(WalkSummary(errors=1))
          raise

  async def request_walk(app, *, reconcile: bool, await_result: bool) -> WalkSummary | None:
      fut = asyncio.get_running_loop().create_future() if await_result else None
      await app.state.walk_queue.put(WalkRequest(reconcile, fut))
      # belt-and-braces timeout so a wedged walk can't hang the request forever
      return await asyncio.wait_for(fut, timeout=30) if fut else None
  ```
  The queue carries only "walk requested" (coarse — every event re-walks the gated tree); a per-path queue is deliberately not built at this scale.
6. **Lifespan: migrate before readiness, reconcile after**
  `create_app`'s lifespan keeps the synchronous `migrate()` before `yield` (a bad schema must block startup), then starts the walk worker and enqueues the initial reconcile *after* `yield` so `/healthz` readiness isn't gated on walking the tree. The worker (and, in Phase 2, the watch) are cancelled and awaited on shutdown for a clean `SIGTERM`. All walker wiring is guarded on `db_path` and `docs_root` being set, so existing `create_app(db_path=None)` tests are unaffected.
  ```python
  @contextlib.asynccontextmanager
  async def lifespan(app):
      if db_path is not None:
          db_path.parent.mkdir(parents=True, exist_ok=True)
          conn = connect(db_path); migrate(conn); conn.close()   # blocks readiness
      worker = watch = None
      if db_path is not None and docs_root is not None:
          app.state.walk_queue = asyncio.Queue()
          worker = asyncio.create_task(_worker(app))
          await request_walk(app, reconcile=True, await_result=False)  # after readiness
          # Phase 2: watch = asyncio.create_task(_watch(app))
      try:
          yield
      finally:
          for task in (watch, worker):
              if task: task.cancel()
          ...  # await with suppress(CancelledError)
  ```
  Readiness means "server up + schema migrated", not "index populated" — downstream `inbox-view` must render an empty index gracefully during cold boot.
  **Connection isolation:** the lifespan's migrate connection is opened and `close()`d before the worker starts; the worker thread opens its own connection per walk. Under WAL, readers (per-request) and the single writer never block on each other, and `busy_timeout=5000` covers any transient lock. Do *not* share one connection across the lifespan and the worker thread — sqlite connections aren't thread-safe to share, and a single serialised worker is what keeps us to one writer.
7. **Config: a docs-root value mirroring `db_path()`**
  ```python
  # config.py
  def docs_root() -> Path:
      override = os.environ.get("FEATURE_SKILLS_WEBAPP_DOCS_ROOT")
      if override:
          p = Path(override).expanduser()
          if not p.is_dir():
              raise ConfigError(f"FEATURE_SKILLS_WEBAPP_DOCS_ROOT is not a directory: {p}")
          return p
      return Path.home() / ".claude" / "feature-docs"   # default; walker tolerates absent
  ```
  Fail-loud only when an explicit override is bad (matching `port()`); a missing *default* just yields an empty walk (logged), since a fresh machine may not have the dir yet.

## File structure

### New files — Phase 1

- `feature_skills_webapp/storage/migrations/0002_documents_status.sql` — DROP+CREATE documents (project_id, nullable feature_id, status, partial unique index); schema_version → 2.
- `feature_skills_webapp/storage/walker.py` — `identity_for`, `parse_doc`, `walk`, `WalkSummary`, upsert helpers.
- `feature_skills_webapp/storage/walker_test.py` — walker unit tests (temp dir + temp DB).
- `feature_skills_webapp/web/discovery.py` — `WalkRequest`, `_worker`, `request_walk`, `_run_walk`.
- `feature_skills_webapp/web/discovery_test.py` — worker coalescing / observes-request semantics.

### Modified files — Phase 1

- `feature_skills_webapp/config.py` + `config_test.py` — add `docs_root()`.
- `feature_skills_webapp/web/app.py` — lifespan: start worker, enqueue initial reconcile, shutdown cleanup; stash `docs_root` on `app.state`.
- `feature_skills_webapp/web/routes.py` + `routes_test.py` — add the `POST /admin/discover` endpoint.
- `feature_skills_webapp/storage/db_test.py` — migration-0002 coverage.
- `feature_skills_webapp/server.py` — pass `config.docs_root()` into `create_app`.
- `pyproject.toml` — add `pytest-asyncio` (dev) + `asyncio_mode = "auto"`.

### Phase 2 / Phase 3

- **P2:** `web/discovery.py` (+ test) — `_watch` via `watchfiles.awatch` + a pure `should_index(path)` filter; `web/app.py` starts the watch task; `pyproject.toml` adds `watchfiles` (runtime).
- **P3:** `storage/walker.py` (+ test) — index `features.html` as a project-level doc and a tolerant tracker-table parser populating `features.status`/`owner`/`notes`.

## Phase 1 — Walker core + index (startup & on-demand)

### What's built

Migration 0002; the synchronous walker (`identity_for`, `parse_doc`, `walk` with incremental + reconcile modes, status transitions, event emission, mtime+size gating); the serialised walk worker and `request_walk`; the lifespan wiring (migrate before readiness, initial reconcile after); the `docs_root()` config; and the `POST /admin/discover` endpoint returning a walk summary. Archive handling (`.feedback-archive/` → `status='archived'`) is included.

### Files touched

See "New / Modified files — Phase 1" above.

### Tests

- `config_test.py`: `docs_root()` default; honours a valid override; raises `ConfigError` on a non-dir override.
- `db_test.py`: 0002 applies on a schema_version-1 DB → version 2; `documents` has `project_id`, nullable `feature_id`, `status`; the partial unique index and both FK indexes exist; `documents` still queryable/empty.
- `walker_test.py`: fresh walk indexes projects/features/documents/events from a temp tree; mtime/size gating skips unchanged files (second walk is a no-op, no new events); a reconcile marks a removed file `missing`; a reappearing file reactivates the same row (`missing` → `active`, child rows preserved); a doc under `.feedback-archive/` indexes as `archived`; a file with no meta tag is skipped; summary counts are correct.
- `discovery_test.py` (deterministic via patching `_run_walk` to block on an `asyncio.Event` + count invocations): two requests enqueued during one in-flight walk coalesce into a single follow-up walk; an on-demand request's future resolves with a summary from a walk that started after its enqueue; a walk that raises is caught (worker survives, summary reports an error); a cancelled worker resolves outstanding futures rather than hanging.
- `routes_test.py`: `POST /admin/discover` against a temp `docs_root` returns 200 with summary counts; returns 503 when discovery is unwired (no `docs_root`); index/healthz tests still pass with the new lifespan.

### MR chain

One MR titled `feat(doc-discovery): phase 1 — walker core + index`.

## Phase 2 — Filesystem watch (live freshness)

### What's built

A `watchfiles.awatch` loop over the docs-root that enqueues incremental walk requests on change, plus a pure `should_index(path)` filter so only `*.html` changes (including under `.feedback-archive/`) enqueue — dotfiles, editor temp/swap (`*.swp`, `*~`) and non-HTML are ignored. Wired into the lifespan after the worker and cancelled cleanly on shutdown. Brought in ahead of tracker parsing so watch edge cases surface early. (`watchfiles`/inotify uses a file descriptor, not a socket, so it stays within the test harness's `--disable-socket` discipline.)

```python
def should_index(path: Path) -> bool:
    if path.suffix != ".html":
        return False
    return not any(part.startswith(".") and part != ".feedback-archive"
                   for part in path.parts)

async def _watch(app) -> None:
    async for changes in awatch(app.state.docs_root):
        if any(should_index(Path(p)) for _change, p in changes):
            await request_walk(app, reconcile=False, await_result=False)
```

### Tests

- `discovery_test.py`: `should_index` is true for an `.html` (incl. inside `.feedback-archive/`) and false for `.swp`/dotfile/non-HTML — deterministic, independent of watch timing.
- Best-effort: a change under a watched temp tree drives an index update (tolerant of timing; the deterministic guarantee is the filter + the enqueue→walk path).

### MR chain

One MR titled `feat(doc-discovery): phase 2 — filesystem watch`.

## Phase 3 — Tracker status (features.html parsing)

### What's built

The walker indexes `<project>/features.html` as a project-level document (`project_id` set, `feature_id` NULL, type `features`), and a tolerant parser reads its tracker tables to upsert `features.status` / `owner` / `notes`. The parser keys on the template's stable section ids (`in-progress`/`available`/`done`) and `td` classes (`feature-name`/`feature-owner`/`feature-notes`), deriving `status` from the section; on a structure it doesn't recognise it logs and skips rather than crashing the walk.

```python
@dataclass(frozen=True)
class TrackerRow:
    slug: str
    status: str           # 'in_progress' | 'available' | 'done' (from the section)
    owner: str | None
    notes: str | None

def parse_tracker(html: str) -> list[TrackerRow]: ...   # tolerant; [] on unrecognised shape
```

### Tests

- `walker_test.py`: a `features.html` indexes as a project-level document with NULL `feature_id` and type `features`; its tracker rows populate `features.status`/`owner`/`notes` (a feature already created bare by a per-feature doc gets back-filled, not duplicated); a hand-mangled tracker yields `[]` and doesn't crash the walk.

### MR chain

One MR titled `feat(doc-discovery): phase 3 — tracker status`.

## QC

The repo has no `CLAUDE.md` yet. Until it does, run the kea-style gate before each commit: `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, and `uv run pytest` (runs under `-n auto` with sockets disabled). If a `CLAUDE.md` is added, follow whatever it specifies at implementation time instead.

## Checklist

### Phase 1: Walker core + index

- Add `docs_root()` to `config.py` (env `FEATURE_SKILLS_WEBAPP_DOCS_ROOT`, default `~/.claude/feature-docs`, fail-loud on a non-dir override); add `config_test.py` cases.
- Write `storage/migrations/0002_documents_status.sql`: DROP+CREATE `documents` with `project_id NOT NULL`, nullable `feature_id`, `status`; recreate `idx_documents_feature`, add `idx_documents_project` + partial unique index on `source_path`; `INSERT schema_version VALUES (2)`.
- Add `db_test.py` coverage: 0002 applies on a v1 DB → version 2; new columns/indexes present; `documents` queryable and empty; `migrate()` idempotent on a v2 DB; mismatch guard triggers at v3+. **Fix the existing** `test_events_survive_document_delete_with_null_fk` — its document INSERT must now supply `project_id` (newly `NOT NULL`) or it fails under 0002.
- Implement `storage/walker.py` scaffolding: `DocIdentity`, `identity_for(rel_path)` (path → project/feature/archived, or None), `ParsedDoc`, `parse_doc(path)` (stdlib `html.parser`; meta tag + title; None if absent), `WalkSummary`.
- Implement the upsert helpers (project, feature, document) with mtime+size gating (size in `metadata_json`), status transitions (created / updated / archived / reactivated), and event emission — all inside `transaction()`.
- Implement `walk(conn, docs_root, *, reconcile)`: recurse `*.html`, upsert each; in reconcile mode mark unseen `active`/`archived` rows `missing` + event; return `WalkSummary`. No-op walk emits no events.
- Write `walker_test.py`: fresh index; gating skip / no-op; reconcile marks missing; reactivation in place; archived doc; missing-meta skip; summary counts.
- Implement `web/discovery.py`: `WalkRequest`, `_run_walk` (own connection), `_worker` (coalesce batch, run one walk via `asyncio.to_thread`, resolve futures, catch+log walk errors), `request_walk`.
- Wire `web/app.py` lifespan: keep synchronous `migrate()` before `yield`; stash `docs_root` on `app.state`; start the worker and enqueue the initial reconcile after `yield`; cancel/await on shutdown; guard on `db_path`/`docs_root`. Update `server.py` to pass `config.docs_root()`.
- Add `POST /admin/discover` in `routes.py`: `request_walk(reconcile=True, await_result=True)`, return the summary as JSON; **guard** — if `app.state.walk_queue` is absent (app built without `docs_root`), return 503. Register the route in `create_app`'s routes list with the existing `async def …(request: Request) -> JSONResponse` shape.
- Add `pytest-asyncio` (dev dep) + `asyncio_mode = "auto"`; write `discovery_test.py` — make it deterministic by patching `_run_walk` to block on an `asyncio.Event` and count invocations (hold one walk in-flight, enqueue a second request, release, assert distinct invocations): covers coalescing, observes-request, and worker-survives-a-failing-walk. Extend `routes_test.py`: `/admin/discover` summary against a temp `docs_root`; 503 when discovery unwired; existing index/healthz still pass.
- Run QC (ruff format/check, ty, pytest); commit and open MR `feat(doc-discovery): phase 1 — walker core + index`.

### Phase 2: Filesystem watch

- Add `watchfiles` to runtime deps in `pyproject.toml`; refresh `uv.lock`.
- Implement `should_index(path)` (pure: `*.html` only; ignore dotfiles/editor-temp; allow `.feedback-archive/`) and `_watch(app)` (`awatch` loop enqueuing incremental walks) in `web/discovery.py`.
- Start the watch task in the `web/app.py` lifespan (after the worker); cancel/await it on shutdown.
- Tests: `should_index` truth table (deterministic); best-effort watch-drives-update test. Run QC; commit and open MR `feat(doc-discovery): phase 2 — filesystem watch`.

### Phase 3: Tracker status

- Extend `identity_for` / `walk` to index `<project>/features.html` as a project-level document (`project_id` set, `feature_id` NULL, type `features`).
- Implement `parse_tracker(html) -> list[TrackerRow]` (tolerant: section ids `in-progress`/`available`/`done` → status; `td` classes → slug/owner/notes; `[]` on unrecognised shape).
- During the walk, when a `features.html` is seen, upsert its features' `status`/`owner`/`notes` (back-fill bare feature rows, don't duplicate).
- Write `walker_test.py` cases (project-level doc with NULL feature; tracker populates feature status/owner/notes; mangled tracker degrades). Run QC; commit and open MR `feat(doc-discovery): phase 3 — tracker status`.
