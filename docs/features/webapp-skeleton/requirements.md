# webapp-skeleton

## Problem

The `feature-skills-webapp` repository is empty. The webapp design doc describes a self-hosted single-tab home for feature docs, broken into nine MVP features — `doc-discovery`, `read-state`, `inbox-view`, `doc-view`, and the rest. Every one of them assumes three things already exist:

- a **running HTTP server** they can hang a route off,
- a **migrated SQLite database** they can read and write,
- a **supervision mechanism** that keeps the process alive so the "single always-open tab" premise holds across reboots and crashes.

None of those exist yet. Without a shared foundation, the next feature (`doc-discovery`) would have to bootstrap its own server, its own database connection layer, and its own way of staying running — and the feature after it would either duplicate that or refactor it. The work is cheap to do once, up front, and expensive to retrofit.

## Vision

A supervised Starlette server runs on `127.0.0.1`, serves a placeholder page and a database-backed health check, and opens a migrated SQLite database carrying the full data model — so the next feature starts by adding a route and a query, not by standing up a stack.

## User stories

1. As Nigel (the operator)
  I want the webapp to start on login and restart on
          crash
  I reboot my machine, open Chrome to the webapp
          tab, and it's already serving — I never have to remember to launch it,
          and a crash overnight self-heals rather than leaving a dead tab.
2. As a future feature author (a Claude Code session)
  I want a migrated database and an app factory already
          in place
  I start

  by adding a
          numbered migration and a route to the existing app — the connection
          pragmas, migration runner, and schema are already there, so I write
          feature code, not plumbing.
3. As Nigel (privacy-conscious operator)
  I want the server reachable only from this machine
  I'm on a shared office network with
          work-in-progress repo docs in the database; nothing I run on

  is reachable by anyone else on the LAN, with no
          auth layer to configure or forget.
4. As a skill that writes docs (later, in feature-skills)
  I want a cheap way to tell whether the webapp can serve
  probes

  before deciding whether to drop its

  call; the probe must report not just "a
          process is listening" but "the database is migrated and the webapp can
          actually serve docs", so a half-broken server doesn't swallow the doc
          silently. The skeleton exposes that endpoint from day one.

## Data model

The skeleton creates the **full data model from §4 of the design doc** in its initial migration, even though every table stays empty until a later feature populates it. The shape:

- **projects** — the repos that have feature docs; **features** belong to a project; **documents** belong to a feature (context, requirements, plan, synthesis rounds, etc.). This is the spine.
- **read_state** — a last-read timestamp per document (drives "new since last visit").
- **synthesis_responses** and **comments** — user input captured against a document (Stage-2 POST targets).
- **events** — an append-only audit log keyed to documents; the inbox and activity feed derive from it.
- **schema_version** — tracks which numbered migrations have been applied, so later features extend the schema without squashing history.

Requirements-level intent only: these are the entities and their relationships. Exact columns, types, indexes, and foreign-key cascade rules are plan-level and live in the migration itself (see indicative notes). The point of laying the whole model now is to give the dependent features a stable, already-designed base to build against rather than negotiating the spine feature-by-feature.

## Technical approach

Mirror the **Starlette + Jinja2 + numbered-SQL-migration** pattern that `kea` uses — the design doc calls it out by name and Nigel is fluent in it. We extend a proven shape rather than inventing one, following kea's SQLite connection conventions (pragmas, transactions, migration runner — detailed in the indicative notes).

- **Server.** A Starlette app built by a `create_app()` factory, bound to `127.0.0.1` on a configurable port (default `8800`). Jinja2 renders a single placeholder page at `/`; a static-files mount is wired up (empty for now) so later features can drop in assets. Run under `uvicorn`.
- **Health check.** A `/healthz` route that reports *readiness*, not just liveness: it runs a cheap `SELECT 1` and returns 200 only when the database is open and migrated. This is the contract `skill-integration-parallel` depends on to decide whether the webapp can be trusted with a doc, so it ships in the skeleton.
- **Storage & connection lifecycle.** A SQLite layer mirroring kea's connection conventions, with the database opened and migrated on startup so first boot creates and migrates it automatically. A **connection is opened per request** (via a Starlette dependency) rather than sharing one long-lived connection — WAL handles concurrent readers cleanly and per-request avoids cross-thread sharing hazards under an async server. This is the pattern later features inherit.
- **Configuration.** Environment variables, set in the systemd unit. The console entry point reads `FEATURE_SKILLS_WEBAPP_PORT` (default `8800`) and `FEATURE_SKILLS_WEBAPP_DB` (default an XDG path, below); an invalid value **fails loud at startup** rather than silently drifting to a default. No config file, no CLI parsing.
- **Database location.** The database lives at an XDG path (`~/.local/share/feature-skills-webapp/db.sqlite`) in every mode — not next to the docs and not repo-local in a dev checkout. In Stage 1 it's a regenerable index over the dev-store files, so there's nothing to back up or chezmoi-sync; a single predictable path also suits a systemd-launched daemon. Revisit at the Stage-2 cutover when SQLite becomes canonical.
- **Supervision.** A `systemd` user unit at `~/.config/systemd/user/feature-skills-webapp.service` that starts on login and restarts on exit (`Restart=always` with a restart backoff and a start-limit so a hard crash loop eventually stops rather than spinning). Chosen over an `~/.config/autostart` entry because autostart only launches on login and never restarts a dead process — the single-tab premise depends on the server staying up. The server logs to stdout/stderr so systemd captures it in the journal (`journalctl --user -u feature-skills-webapp`), and handles `SIGTERM` cleanly — draining requests and closing the database — on logout or restart.
- **README.** The repo ships a README (design-doc §8 requirement): opens with "companion to feature-skills", links back to the design doc, documents the `systemctl --user enable --now` bootstrap, and pins a minimum `feature-skills` version (`v2.1`).
- **Runtime.** Target Python 3.14, `uv`-managed. Don't hard-pin a patch version in a way that breaks on minor bumps — use what mise provides (per the context doc).

**Out of scope for the skeleton:** HTMX (no interactive page needs it yet — vendor it offline-safe when the first feature does), SSE, the MCP bridge, doc discovery, and any real rendering. The skeleton proves the stack; it does nothing useful on its own, and that's fine.

## Testing

The skeleton establishes the test harness every later feature uses, mirroring kea's setup:

- **pytest with `pytest-xdist`** (`-n auto`) for parallel runs, and colocated `*_test.py` files alongside the code (kea convention).
- **`pytest-socket`** with sockets disabled (unix sockets allowed) so tests can't accidentally reach the network — which also enforces the local-only discipline at test time.
- **One database per xdist worker.** A root `conftest.py` points each worker at its own temp DB via the `FEATURE_SKILLS_WEBAPP_DB` override (keyed on `PYTEST_XDIST_WORKER`) *before* any app import resolves, so parallel workers don't contend on one SQLite writer lock.
- **Busy timeout** in the connection pragmas (carried over from kea) so the loser of a two-writer race waits rather than failing instantly with `database is locked` under contention.

Each delivery phase lands with its own tests; the harness itself is set up in Phase 1 alongside the first route.

## Alternatives considered

1. Grow the schema feature-by-feature instead of laying §4 up front
  Source: numbered-migration principle in the context doc
  Each dependent feature would add only the tables
          it needs via its own migration — maximally lean. Rejected because the
          §4 model is already designed as a coherent whole and the design doc
          explicitly asks for it in the initial migration; fragmenting it would
          make every dependent feature start with schema negotiation. Numbered
          migrations still let later features add columns and indexes they
          discover they need — laying the spine now doesn't freeze it.
2. ~/.config/autostart entry for supervision
  Source: design doc open questions
  Simpler than a systemd unit, but only launches on
          login — it doesn't restart the process on crash. The "single
          always-open tab" premise needs the server to stay up, so systemd's

  wins.
3. DB next to the docs (~/.claude/feature-docs/_webapp.db), or repo-local in a dev checkout
  Source: design doc open questions; kea's config.py precedence
  The feature-docs location keeps everything in one
          chezmoi-managed tree; kea's pattern would put the DB in the repo root
          when run from a source checkout. Both rejected for the webapp: the
          Stage-1 DB is a regenerable index (syncing a cache is pointless churn),
          and a systemd daemon wants one predictable path regardless of how it's
          launched — so we use a single XDG location everywhere and skip kea's
          source-checkout fork. The trade flips at Stage 2 when the DB holds
          canonical content; revisit then.
4. Auto-hunt for a free port when 8800 is taken
  Source: review round 1
  Would dodge "address in use" crash loops, but
          other features (and skills) hardcode

  for
          detection — a server that silently moved ports would break them.
          Instead we emit a clear startup error and let the operator relocate via

  ; the systemd start-limit keeps
          a stuck loop from spinning.

## Delivery phases

The design doc sizes this feature as "small". Three thin phases, each one MR, each independently testable — ordered so value lands early (a running server) and supervision wraps a thing that already works. Each phase lands with tests.

### Phase 1 — Running server + project scaffold

The Starlette app factory, the `/` placeholder page, configuration (port + loopback bind), the static mount, the console entry point — run under uvicorn — plus the repo scaffold: the README (per §8) and the pytest harness (xdist + pytest-socket). No database yet, so the entry point launches the server without opening a DB at this stage. **Testable:** `curl 127.0.0.1:8800/` returns 200 and contains a known marker string; the port honours its env var and fails loud on a bad value; the socket binds loopback only.

### Phase 2 — Persistence + readiness health check

The SQLite connection layer (pragmas, transaction helper, migration runner, per-request connection), the initial numbered migration establishing the full §4 schema plus `schema_version`, the DB-open-and-migrate-on-startup wiring, and the `/healthz` readiness endpoint. **Testable:** a fresh boot creates the DB at the XDG path, applies the migration, leaves `schema_version` at 1, and every §4 table exists and is queryable (empty); `/healthz` returns 200 when migrated and non-200 when the DB is unavailable.

### Phase 3 — Supervision

The systemd user unit: starts on login, `Restart=always` with backoff and a start-limit, configuration via environment, logs to the journal, clean `SIGTERM` shutdown. **Testable:** `systemctl --user enable --now` brings it up; status shows it active; killing the process sees it restart; the journal shows startup and shutdown lines; a clean stop drains and closes the DB; an occupied port surfaces a clear error rather than a silent loop.

## Indicative implementation notes

Plan-level detail worth carrying forward to `/feature-plan`. Most of it is "do what kea does":

- **Storage layer (port kea's `storage/db.py` closely).** `connect()` opens with `isolation_level=None` (autocommit) and an `executescript` of pragmas — carry kea's full set: `journal_mode=WAL`, `synchronous=NORMAL`, `temp_store=MEMORY`, `cache_size=-20000`, `mmap_size`, `foreign_keys=ON`, `busy_timeout=5000` — plus the runtime guards asserting WAL and FK actually landed. A `transaction()` context manager wraps `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` (the stdlib `with conn:` form is a silent no-op under autocommit — don't use it). `migrate()` globs `migrations/NNNN_*.sql` sorted, applies the unapplied ones inside one `BEGIN IMMEDIATE`, and raises a `SchemaVersionMismatchError` if the DB version exceeds the highest migration on disk. `open_db()` mkdirs the parent, connects, migrates, yields. *Caveat:* kea's runner splits SQL on `;` naively — fine for the plain §4 DDL, but a sharp edge for future migrations containing triggers or semicolons inside string literals; flag it when porting.
- **App factory (port kea's `web/app.py`).** `create_app(db_path)` builds a Jinja `Environment(FileSystemLoader, select_autoescape(["html"]))`, defines the routes list, stashes `jinja` and `db_path` on `app.state`, and mounts `/static` via `StaticFiles`. Open the per-request connection in a Starlette dependency/middleware that closes it on response.
- **Config (port kea's `config.py`, minus the source-checkout fork).** Env-var override first, then a single XDG default — do *not* reproduce kea's "pyproject present → repo-local DB" branch; the webapp uses `~/.local/share/feature-skills-webapp/db.sqlite` everywhere. The port reader defaults to `8800` and raises on a non-integer/out-of-range value.
- **Schema (migration `0001_init.sql`).** Use 4-digit zero-padded migration prefixes (consistent width so the sorted glob never mis-orders). The seven §4 tables + `schema_version` with an `INSERT … VALUES (1)` at the end, following kea's `001_initial.sql` convention. Timestamps as ISO-8601 TEXT (kea convention). Foreign keys with `ON DELETE CASCADE` down the projects → features → documents spine.
- **Entry point.** A `[project.scripts]` console entry (e.g. `feature-skills-webapp`) that opens/migrates the DB and launches uvicorn bound to `127.0.0.1:<port>`. (Phase 1 lands the no-DB version; Phase 2 adds the open/migrate step to the same entry point.)
- **Project tooling.** Python 3.14, `uv`-managed (mirror kea's `pyproject.toml` / `uv.lock` / `hatch_build.py` setup), but don't over-pin the patch version. Package layout following kea (a package dir with `web/` + `storage/migrations/` rather than the looser `app/`/`db/`/`bin/` sketch in the context doc — kea's layout is the live reference).
- **Test harness (mirror kea).** Dev deps `pytest`, `pytest-xdist`, `pytest-socket` (add `pytest-asyncio` if async tests appear). `[tool.pytest.ini_options] addopts = "--import-mode=importlib -n auto --disable-socket --allow-unix-socket"`. A root `conftest.py` sets `FEATURE_SKILLS_WEBAPP_DB` to a per-worker temp path keyed on `PYTEST_XDIST_WORKER` before app imports resolve, and exposes a `temp_db` fixture (connect+migrate a fresh DB).
- **systemd unit.** `~/.config/systemd/user/feature-skills-webapp.service`: `ExecStart` the console entry, `Environment=` lines for port + DB path, `Restart=always`, `RestartSec=` backoff, `StartLimitIntervalSec`/`StartLimitBurst` to cap a crash loop, `WantedBy=default.target`. Logs go to the journal by default. Decide whether the unit file is committed as a template and symlinked, or generated by an install script — a question for the plan.

## Design notes

- **Database location → XDG, single path everywhere** (round 1). Confirmed dropping kea's source-checkout fork: the Stage-1 DB is a regenerable index, and a systemd daemon wants one predictable path. Revisit at Stage 2 when the DB becomes canonical.
- **`/healthz` is a readiness check** (round 1). It touches the DB (`SELECT 1`) so it reports "can actually serve", which is what the downstream skill needs. The trailing `z` follows the Kubernetes/Google convention (`/healthz`, `/readyz`, `/livez`) — it namespaces machine-only endpoints away from content routes so a future real `/health` page can't collide.
- **Connection-per-request** (round 1). The skeleton sets this pattern for all later features; chosen over a shared connection to avoid cross-thread hazards under the async server, relying on WAL for concurrent reads.
- **Supervision = `Restart=always`** (round 1). Upgraded from `on-failure` so a clean-but-unexpected exit still restarts; paired with a start-limit so a genuine crash loop stops.
- **Test discipline** (round 1, Nigel). pytest + `pytest-socket` (no network) + `pytest-xdist` with a per-worker DB, plus the connection busy-timeout — the kea pattern, set up in the skeleton so every later feature inherits it.
- **Config bad-value handling** (round 1). Invalid port/DB env values fail loud at startup rather than silently falling back — a misconfigured unit should surface, not drift.
