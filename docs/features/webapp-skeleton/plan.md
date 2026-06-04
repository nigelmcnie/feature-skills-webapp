# webapp-skeleton

## Overview

Stand up the `feature-skills-webapp` repo as a supervised Starlette server on `127.0.0.1:8800` that serves a placeholder page and a database-backed `/healthz` readiness check, opens a migrated SQLite database carrying the full §4 data model, and runs under a systemd user unit that restarts on exit. The stack mirrors `kea`'s Starlette + Jinja2 + numbered-SQL-migration pattern closely — connection pragmas, the `transaction()` helper, and the migration runner are near-direct ports. Delivered in three thin, independently testable MRs: running server + scaffold, then persistence + health check, then supervision.

## Key technical decisions

1. **Package layout mirrors kea (`web/` + `storage/migrations/`)**
  Importable package `feature_skills_webapp` (the distribution name `feature-skills-webapp` can't be a Python identifier). Use kea's split — a `web/` subpackage for the Starlette app and routes, a `storage/` subpackage for the DB layer and numbered migrations — rather than the looser `app/`/`db/`/`bin/` sketch in the context doc. The console entry point lives in `server.py`.
2. **Storage layer is a near-direct port of kea's `storage/db.py`**
  Carry kea's full pragma set and the autocommit + explicit-transaction design verbatim. The migration runner globs `NNNN_*.sql` (4-digit prefixes here), applies unapplied files inside one `BEGIN IMMEDIATE`, and records progress in `schema_version`.
  ```python
  def connect(path: Path) -> sqlite3.Connection:
      conn = sqlite3.connect(path, isolation_level=None)  # autocommit
      conn.row_factory = sqlite3.Row
      conn.executescript(
          "PRAGMA journal_mode=WAL;"
          "PRAGMA synchronous=NORMAL;"
          "PRAGMA temp_store=MEMORY;"
          "PRAGMA cache_size=-20000;"
          "PRAGMA mmap_size=67108864;"
          "PRAGMA foreign_keys=ON;"
          "PRAGMA busy_timeout=5000;"
      )
      if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
          raise RuntimeError("expected WAL")
      if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
          raise RuntimeError("expected foreign_keys=ON")
      return conn

  @contextmanager
  def transaction(conn): ...        # BEGIN IMMEDIATE / COMMIT / ROLLBACK
  def current_version(conn) -> int: ...
  def migrate(conn, migrations_dir=MIGRATIONS_DIR) -> int: ...   # raises SchemaVersionMismatchError
  @contextmanager
  def open_db(path) -> Iterator[sqlite3.Connection]: ...        # mkdir, connect, migrate, yield
  ```
  Copy kea's guard structure verbatim — including the `if migration_files:` wrap around the `max_available` check — rather than paraphrasing it.
  **Caveat to carry forward:** kea's `migrate()` splits each file on `;` naively. Fine for the plain §4 DDL, but a sharp edge for any future migration containing triggers or semicolons in string literals. Keep the port faithful but leave a comment flagging it.
3. **Connection-per-request via a small context-manager helper**
  Starlette has no FastAPI-style DI. A handler opens a short-lived connection and closes it on the way out; WAL handles concurrent readers. This is the pattern later features inherit. The DB path lives on `app.state.db_path`; the helper lives in `web/db_dep.py`.
  **Deliberate deviation** from the requirements' wording ("via a Starlette dependency/middleware"): a handler-scoped context manager is preferable to middleware, which would open a connection for *every* route including static assets, wastefully. Same per-request lifecycle, less overhead.
  ```python
  @contextmanager
  def request_conn(app) -> Iterator[sqlite3.Connection]:
      conn = connect(app.state.db_path)
      try:
          yield conn
      finally:
          conn.close()
  ```
4. **Config from env vars, XDG DB default, fail-loud port parsing**
  A single XDG path in every mode — no kea-style source-checkout fork. An invalid port surfaces at startup rather than drifting to a default.
  ```python
  DEFAULT_PORT = 8800

  class ConfigError(Exception): ...

  def db_path() -> Path:
      override = os.environ.get("FEATURE_SKILLS_WEBAPP_DB")
      if override:
          return Path(override)
      xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
      return xdg / "feature-skills-webapp" / "db.sqlite"

  def port() -> int:
      raw = os.environ.get("FEATURE_SKILLS_WEBAPP_PORT")
      if not raw:
          return DEFAULT_PORT
      try:
          value = int(raw)
      except ValueError as e:
          raise ConfigError(f"FEATURE_SKILLS_WEBAPP_PORT must be an integer, got {raw!r}") from e
      if not (1 <= value <= 65535):
          raise ConfigError(f"FEATURE_SKILLS_WEBAPP_PORT out of range: {value}")
      return value
  ```
5. **App factory; migrate on startup via lifespan; readiness `/healthz`**
  `create_app(db_path)` builds the Jinja env, routes, static mount, and a lifespan handler that ensures the DB dir exists and runs `migrate()` on startup. `/healthz` opens a per-request connection and runs `SELECT 1` — 200 only when the DB is reachable and migrated, else 503.
  ```python
  def create_app(db_path: Path | None) -> Starlette:
      jinja = Environment(loader=FileSystemLoader(TEMPLATES_DIR),
                          autoescape=select_autoescape(["html"]))

      @contextlib.asynccontextmanager
      async def lifespan(app):
          if db_path is not None:
              db_path.parent.mkdir(parents=True, exist_ok=True)
              conn = connect(db_path); migrate(conn); conn.close()
          yield

      app = Starlette(routes=[Route("/", index), Route("/healthz", healthz)],
                      lifespan=lifespan)
      app.state.jinja = jinja
      app.state.db_path = db_path
      app.mount("/static", StaticFiles(directory=STATIC_DIR))
      return app

  async def healthz(request):
      try:
          with request_conn(request.app) as conn:
              conn.execute("SELECT 1")
      except Exception:
          return JSONResponse({"status": "unavailable"}, status_code=503)
      return JSONResponse({"status": "ok"})
  ```
  The `z` suffix follows the k8s/Google convention so a future real `/health` content route can't collide. (Phase 1 ships `create_app` with `db_path=None` and only the index route; Phase 2 adds the lifespan migrate + `/healthz`.)
  **Failure intent:** if `migrate()` raises during lifespan startup (e.g. `SchemaVersionMismatchError` or a DDL error), let it propagate — the process should crash loudly, caught by the systemd start-limit, rather than be wrapped in a `try/except` that hides a bad DB.
6. **Console entry point runs uvicorn bound to loopback**
  A plain `main()` reads config and serves — a deliberate divergence from kea's Typer CLI, since the webapp has no subcommands. `uvicorn` handles `SIGTERM` graceful shutdown out of the box (drains in-flight requests). Because connections are per-request, no DB handle is held open at shutdown — there's nothing to close; WAL's `-wal`/`-shm` sidecars persist harmlessly and auto-checkpoint on next open.
  ```python
  # feature_skills_webapp/server.py
  def main() -> None:
      import uvicorn
      from feature_skills_webapp import config
      from feature_skills_webapp.web.app import create_app
      app = create_app(config.db_path())
      uvicorn.run(app, host="127.0.0.1", port=config.port(), log_level="info")

  # pyproject.toml
  # [project.scripts]
  # feature-skills-webapp = "feature_skills_webapp.server:main"
  ```
7. **systemd user unit, committed as a template, `Restart=always`**
  The unit lives in the repo at `systemd/` and the README documents symlinking it into `~/.config/systemd/user/` (no install script for v1 — keep it manual and obvious). `Restart=always` with a backoff and a start-limit so a hard crash loop eventually stops. Logs go to the journal via stdout/stderr.
  ```ini
  [Unit]
  Description=feature-skills webapp
  After=default.target
  # StartLimit* are [Unit]-only keys — in [Service] systemd ignores them
  # and falls back to the manager defaults (10s/5).
  StartLimitIntervalSec=60
  StartLimitBurst=5

  [Service]
  Type=simple
  Environment=FEATURE_SKILLS_WEBAPP_PORT=8800
  ExecStart=%h/.local/bin/feature-skills-webapp
  Restart=always
  RestartSec=2

  [Install]
  WantedBy=default.target
  ```
8. **Test harness mirrors kea: xdist + pytest-socket + per-worker DB**
  A root `conftest.py` points each xdist worker at its own DB via the `FEATURE_SKILLS_WEBAPP_DB` override *before* app imports resolve, and exposes a `temp_db` fixture. Sockets are disabled (unix sockets allowed) to enforce no-network at test time. Web handlers are tested with Starlette's `TestClient`.
  ```python
  # conftest.py
  _worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
  os.environ.setdefault(
      "FEATURE_SKILLS_WEBAPP_DB",
      str(Path(tempfile.gettempdir()) / f"fsw-test-{_worker}.db"),
  )

  @pytest.fixture
  def temp_db(tmp_path):
      db = tmp_path / "test.db"
      conn = connect(db); migrate(conn); conn.close()
      return db

  # pyproject: addopts = "--import-mode=importlib -n auto --disable-socket --allow-unix-socket"
  ```

## File structure

### New files — Phase 1 (scaffold + server)

- `pyproject.toml` — project metadata, deps, `requires-python = ">=3.14"`, hatchling build with `[tool.hatch.build.targets.wheel] packages = ["feature_skills_webapp"]` and test-file `exclude`, `[project.scripts]`, ruff/ty/pytest config (mirror kea). No `hatch_build.py` — the webapp has no generated artefacts, so kea's custom build hook is intentionally dropped.
- `uv.lock` — committed lockfile (uv-managed, mirror kea).
- `README.md` — companion-to-feature-skills, design-doc link, systemctl bootstrap, `feature-skills` ≥ v2.1 pin.
- `conftest.py` — per-worker DB env override (the `temp_db` fixture is added in Phase 2).
- `feature_skills_webapp/__init__.py`
- `feature_skills_webapp/config.py` — `port()`, `db_path()`, `ConfigError`.
- `feature_skills_webapp/server.py` — `main()` console entry.
- `feature_skills_webapp/config_test.py` — port parsing + db_path resolution.
- `feature_skills_webapp/server_test.py` — `main()` binds `127.0.0.1` (monkeypatched uvicorn).
- `feature_skills_webapp/web/__init__.py`
- `feature_skills_webapp/web/app.py` — `create_app()` factory.
- `feature_skills_webapp/web/routes.py` — `index` handler.
- `feature_skills_webapp/web/routes_test.py` — index returns 200 + marker.
- `feature_skills_webapp/web/templates/index.html` — placeholder page with a known marker string.
- `feature_skills_webapp/web/static/.gitkeep` — empty static mount target.
- `.gitignore`, `.python-version` (3.14, not over-pinned).

### New files — Phase 2 (persistence)

- `feature_skills_webapp/storage/__init__.py`
- `feature_skills_webapp/storage/db.py` — `connect`, `transaction`, `current_version`, `migrate`, `open_db`, `SchemaVersionMismatchError`.
- `feature_skills_webapp/storage/db_test.py` — pragmas, migrate idempotency, schema_version, mismatch, table existence.
- `feature_skills_webapp/storage/migrations/0001_init.sql` — full §4 schema + `schema_version`.
- `feature_skills_webapp/web/db_dep.py` — `request_conn()` per-request connection helper (web-layer glue).

### New files — Phase 3 (supervision)

- `systemd/feature-skills-webapp.service` — committed unit template.

### Modified files

- `feature_skills_webapp/web/app.py` — Phase 2 adds lifespan migrate + `/healthz` route.
- `feature_skills_webapp/web/routes.py` — Phase 2 adds `healthz` handler.
- `conftest.py` — Phase 2 adds the `temp_db` fixture.
- `README.md` — Phase 3 adds the systemd install/verify section.

## Phase 1 — Running server + project scaffold

### What's built

The repo's bones: `pyproject.toml` with the kea-style toolchain (ruff, ty, pytest + xdist + pytest-socket), the package skeleton, the config module (port fail-loud, XDG db path defined but not yet used), the Starlette app factory with a single `/` placeholder route and static mount, the `main()` console entry running uvicorn on loopback, the README, and the pytest harness. No database is opened at this stage — `create_app(None)`.

### Files touched

See "New files — Phase 1" above.

### Tests

- `config_test.py`: `port()` returns 8800 when unset/empty; parses a valid value; raises `ConfigError` on non-integer and out-of-range; `db_path()` honours the env override and falls back to the XDG path.
- `routes_test.py`: `TestClient` GET `/` returns 200 and the body contains the known marker string.
- `server_test.py`: monkeypatch `uvicorn.run` and call `main()`; assert it's invoked with `host="127.0.0.1"` and the configured port. (The loopback bind isn't exercisable through `create_app`; this guards the requirement that the socket is loopback-only.)

### Manual check

`FEATURE_SKILLS_WEBAPP_PORT=8800 feature-skills-webapp`, then `curl -s 127.0.0.1:8800/` shows the placeholder; a bad port value exits with a clear error.

### MR chain

One MR titled `feat(webapp-skeleton): phase 1 — running server + scaffold`.

## Phase 2 — Persistence + readiness health check

### What's built

The SQLite layer (`storage/db.py`, ported from kea), the initial migration establishing the full §4 schema plus `schema_version`, the lifespan hook that creates the DB dir and migrates on startup, the per-request connection helper, and the `/healthz` readiness route. `create_app` is now called with the real `db_path()`.

### Schema (`0001_init.sql`)

Seven tables on the projects → features → documents spine plus the satellites, FKs `ON DELETE CASCADE` down the spine, ISO-8601 TEXT timestamps, and the version row:

```sql
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    repo_path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    status TEXT, owner TEXT, notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (project_id, slug)
);
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id INTEGER NOT NULL REFERENCES features(id) ON DELETE CASCADE,
    type TEXT NOT NULL, source_path TEXT, content_html TEXT,
    metadata_json TEXT, source_mtime TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE read_state (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    last_read_at TEXT NOT NULL
);
CREATE TABLE synthesis_responses (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    item_num INTEGER NOT NULL, response TEXT, routine_flag TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (document_id, item_num)
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    excerpt TEXT, text TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, integrated_at TEXT
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- SET NULL, not CASCADE: events is an append-only audit log, so the
    -- history must survive a document being deleted (it's a satellite,
    -- not part of the projects→features→documents cascade spine).
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL, payload_json TEXT, created_at TEXT NOT NULL
);

-- FK indexes (the inbox/activity queries traverse these).
CREATE INDEX idx_features_project ON features(project_id);
CREATE INDEX idx_documents_feature ON documents(feature_id);
CREATE INDEX idx_events_document ON events(document_id);

CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version (version) VALUES (1);
```

Exact column nullability is the planner's to firm up against §4; the relationships, the cascade spine, the `events` SET-NULL audit semantics, and the FK indexes are the load-bearing parts.

### Tests

- `db_test.py`: `connect()` lands WAL + FK on; `migrate()` on a fresh DB returns 1 and is idempotent on a second call; `schema_version` holds 1; a stamped version above the max raises `SchemaVersionMismatchError`; every §4 table is present and empty (`SELECT` succeeds); an FK cascade deletes children.
- `routes_test.py` (extended): `/healthz` returns 200 against a migrated DB; returns 503 when the DB is unreachable — force this deterministically by pointing `app.state.db_path` at a *directory* (so `sqlite3.connect` raises), not the vaguer "unwritable" case which can still open read-only.

### MR chain

One MR titled `feat(webapp-skeleton): phase 2 — persistence + health check`.

## Phase 3 — Supervision

### What's built

The committed systemd user unit and the README section documenting how to install and operate it. No application code changes beyond confirming clean `SIGTERM` behaviour. This phase is largely operational/manual verification, since systemd integration isn't unit-testable in CI.

### Files touched

`systemd/feature-skills-webapp.service` (new), `README.md` (install + verify section).

### Verification (manual, documented in README)

- Symlink the unit into `~/.config/systemd/user/`, then `systemctl --user daemon-reload && systemctl --user enable --now feature-skills-webapp`.
- `systemctl --user status` shows active; `curl 127.0.0.1:8800/healthz` returns 200.
- `kill` the worker → systemd restarts it; `journalctl --user -u feature-skills-webapp` shows startup/shutdown lines.
- Clean stop (`systemctl --user stop`): uvicorn drains in-flight requests and exits without error. (No long-lived DB handle is held — connections are per-request — so there's nothing to close; WAL sidecars persist harmlessly.)
- Occupy 8800 with another process → the unit logs a clear "address in use" error and the start-limit halts the loop rather than spinning forever.
- Log out / log back in (or restart the user manager) → the service comes back on its own.

### MR chain

One MR titled `feat(webapp-skeleton): phase 3 — supervision`.

## QC

The repo has no `CLAUDE.md` yet. Until it does, run the kea-style gate before each commit: `ruff format`, `ruff check`, the type checker (`ty`), and `pytest` (which runs under `-n auto` with sockets disabled). If a `CLAUDE.md` is added to the repo (a reasonable Phase 1 addition capturing these conventions), the implementing agent should follow whatever it says at implementation time instead.

## Checklist

### Phase 1: Running server + scaffold

- Write `pyproject.toml`: deps (starlette, jinja2, uvicorn), dev deps (pytest, pytest-xdist, pytest-socket, ruff, ty; add pytest-asyncio if async tests need it), `requires-python = ">=3.14"`, hatchling build with `packages = ["feature_skills_webapp"]` + test-file `exclude` (no `hatch_build.py`), `[project.scripts] feature-skills-webapp = "feature_skills_webapp.server:main"`, and pytest/ruff/ty config mirroring kea (including `addopts = "--import-mode=importlib -n auto --disable-socket --allow-unix-socket"`). Commit `uv.lock`. Add `.gitignore`, `.python-version`.
- Create the package skeleton: `feature_skills_webapp/{__init__,config,server}.py`, `web/{__init__,app,routes}.py`, `web/templates/index.html`, `web/static/.gitkeep`.
- Implement `config.py`: `DEFAULT_PORT=8800`, `ConfigError`, `port()` (fail-loud on non-int/out-of-range), `db_path()` (env override → XDG default, no source-checkout fork).
- Implement `web/app.py` `create_app(db_path)`: Jinja env, index route, `/static` mount, `app.state`. For Phase 1, `db_path` may be `None` and no lifespan/DB wiring yet.
- Implement `web/routes.py` `index` handler rendering `index.html` with a known marker string.
- Implement `server.py` `main()`: read config, `uvicorn.run(create_app(...), host="127.0.0.1", port=config.port())`.
- Add root `conftest.py` with the per-worker `FEATURE_SKILLS_WEBAPP_DB` override set before app imports resolve.
- Write `config_test.py`, `web/routes_test.py` (TestClient: `/` → 200 + marker), and `server_test.py` (monkeypatch `uvicorn.run`; assert `host="127.0.0.1"` + configured port — the loopback-only guard).
- Write `README.md`: "companion to feature-skills", link the design doc, document the systemctl bootstrap, pin `feature-skills` ≥ v2.1.
- Run QC (ruff format/check, ty, pytest); verify `curl /` manually; commit and open MR `feat(webapp-skeleton): phase 1`.

### Phase 2: Persistence + health check

- Implement `storage/db.py` ported from kea: `connect()` (full pragma set + WAL/FK guards), `transaction()`, `current_version()`, `migrate()` (4-digit glob, `schema_version`, `SchemaVersionMismatchError`), `open_db()`. Comment the naive `;`-split caveat.
- Write `storage/migrations/0001_init.sql`: the seven §4 tables + `schema_version` (INSERT 1), FKs `ON DELETE CASCADE` down projects → features → documents, `events.document_id ON DELETE SET NULL` (audit log survives doc deletion), FK indexes on `features.project_id` / `documents.feature_id` / `events.document_id`, ISO TEXT timestamps.
- Add `request_conn()` per-request connection helper.
- Wire the lifespan startup hook in `create_app`: mkdir the DB parent and `migrate()` on boot; call `create_app(config.db_path())` from `main()`.
- Add the `/healthz` readiness handler (SELECT 1 → 200, else 503) and register the route.
- Add the `temp_db` fixture to `conftest.py`; write `storage/db_test.py` (WAL/FK, migrate idempotency, schema_version, mismatch, table existence, cascade) and extend route tests for `/healthz` 200/503.
- Run QC; verify a fresh boot creates + migrates the DB at the XDG path; verify the migration `.sql` ships in the built wheel (since `migrate()` globs it at runtime via `__file__`); commit and open MR `feat(webapp-skeleton): phase 2`.

### Phase 3: Supervision

- Write `systemd/feature-skills-webapp.service`: `ExecStart` the console entry, `Environment=` port (+ DB path if overriding), `Restart=always`, `RestartSec`, `StartLimitIntervalSec`/`StartLimitBurst`, `WantedBy=default.target`.
- Add the README install + verify section (symlink into `~/.config/systemd/user/`, `daemon-reload`, `enable --now`, `journalctl` diagnosis).
- Manually verify: enable/status, healthz 200, kill→restart, clean stop drains/closes DB, port-in-use clear error + start-limit halts loop, logout/login persistence.
- Commit and open MR `feat(webapp-skeleton): phase 3`.
