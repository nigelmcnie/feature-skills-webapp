# doc-discovery

## Problem

`webapp-skeleton` shipped a running server and a migrated SQLite database carrying the full §4 schema — but every table is empty. The webapp can't show an inbox, a project view, or even a single doc, because it has no model of *what docs exist*. That model lives entirely on disk today: a growing tree of static HTML under `~/.claude/feature-docs/<project>/<feature>/` (at the time of writing ~56 docs across `kea`, `planning` and `feature-skills-webapp`), discoverable only by `grep` or Chrome history.

Walking the filesystem on every request would be slow and would couple every view to disk layout. The webapp needs a queryable index: rows in `projects` / `features` / `documents` that mirror what's on disk, kept fresh as skills write new docs, with an `events` row per change so the inbox can later answer "what's new since I last looked?". Nothing writes those rows yet — `doc-discovery` is the writer.

## Vision

A filesystem walker indexes the dev-store into SQLite — on startup, on demand, and as files change — so every later view queries a fresh, cheap index instead of the disk, and the index can be rebuilt from the files at any time.

## User stories

1. As the next feature author (inbox-view / doc-view)
  I want a queryable index of projects, features and documents
  I'm building the inbox. I want to

  documents and their latest events grouped by project, not recurse a
        directory tree on every page load.

  populated the
        tables, so my view is a query.
2. As Nigel (operator)
  I want docs a skill just wrote to appear in the webapp within seconds, without telling it
  A

  run writes

  into the dev-store. The filesystem watch
        notices and my always-open webapp tab reflects it almost immediately — no
        manual "re-scan" step, no restart.
3. As Nigel (operator)
  I want archived synthesis docs kept out of the live inbox but still findable
  Last week's

  was moved to

  . It shouldn't clutter "what
        needs my attention", but I still want it in history when I go looking.
4. As Nigel (operator)
  I want to force a re-index on demand and see what it did
  I hand-edited a doc, or the watch looks like it missed
        a change. I hit an admin endpoint; it reconciles with disk and returns a
        summary (how many docs created / updated / archived / gone-missing /
        errored) so I know it worked.
5. As Nigel (privacy-conscious operator)
  I want discovery to stay entirely local and read-only against my docs
  The walker only reads files under the dev-store and
        writes to the local SQLite index. It never phones home and never modifies a
        source doc — the files stay canonical in Stage 1.

## Data model

`doc-discovery` is the first writer to the spine the skeleton laid: `projects` → `features` → `documents`, plus the `events` satellite. Everything it writes is **derived from the filesystem** and therefore disposable — the index is a cache over the dev-store, never the source of truth (that flips at Stage 2).

- **Identity comes from the path.** `<docs-root>/<project>/<feature>/<doc>.html` yields the project name and feature slug; the doc's `<meta name="feature-doc-type">` yields its `type`. Because `features.project_id` is `NOT NULL`, the walker **upserts `projects` and `features` rows** to have somewhere to hang a document — it owns creating the whole spine, not just leaf rows.
- **A document may belong to a project, not a feature.** `features.html` lives at the project root with no feature slug, and we may want other non-feature-scoped docs here in future. So `documents.feature_id` becomes **nullable** (migration `0002`): a project-level doc carries its `project` but a NULL feature.
- **A `status` on each document.** A document is `active`, `archived` (moved into `.feedback-archive/` — kept in history, hidden from live views), or `missing` (its source file has gone from disk). This single `status` column (migration `0002`) replaces both a plain "archived" boolean and any hard-delete: vanished docs are *marked*, not deleted (see Design notes for why).
- **Reactivation in place.** A `missing` row whose `source_path` reappears is revived on the existing row (status → `active`, a `reactivated` event emitted) rather than re-inserted — the partial unique index forbids a second row on that path, and reusing the row preserves any child read-state/comments. Consequence: a genuinely *different* doc landing at a reused path inherits the prior row's identity; accepted in Stage 1, since the index is regenerable and an explicit rebuild fixes any mismatch.
- **Change detection by mtime (and size).** Each document row records the source file's modification time; a walk only re-reads and re-writes a doc when its mtime (or size) has moved, so repeated walks over an unchanged store are cheap. A walk that changes nothing emits no events.
- **One event per change.** Every discovered create / update / archive / mark-missing appends an `events` row. This is the data the inbox's "new since last visit" derives from — emitting it is core to the feature, not incidental logging.
- **Content stays on disk.** In Stage 1 the walker indexes *metadata only*: it reads each file to extract the meta tag and the `<title>` (stored in `metadata_json` for display), but leaves `content_html` empty. `doc-view` renders from the source file. Slurping content is a Stage-2 concern.
- **`source_path` is the upsert key.** It must be unique for file-backed docs — migration `0002` adds a partial unique index (`WHERE source_path IS NOT NULL`, leaving Stage-2 content-only docs legal).
- **`features.status` / `owner` / `notes`** come from parsing the project's `features.html` tracker (a later phase) — they exist nowhere else. **`projects.repo_path` stays empty**: the dev-store path gives the project *name*, not its git location, and nothing in Stage 1 needs it.

## Technical approach

A **walker module** in the `storage` layer recurses the dev-store root, and for each `.html` file: derives the project/feature from its path, reads the `feature-doc-type` meta tag (via the standard library — no new parsing dependency), and upserts `projects`/`features`/`documents` inside the existing `transaction()` helper, mtime/size-gated so unchanged files are skipped. Each write that actually changes a row appends an `events` row; a no-op walk writes nothing.

**Two walk modes, one core.** An *incremental* walk upserts what it finds (used by the watch). A *full reconcile* additionally marks index rows whose source file no longer exists as `status='missing'` and emits an event — deletions and renames are handled by marking, never by hard-deleting rows (which would cascade-destroy the read-state, comments and synthesis data that later features hang off `documents`). Because the index is regenerable, an explicit admin "rebuild" (drop-all + re-walk) remains available as the nuclear option.

**One writer, many triggers.** doc-discovery introduces the webapp's first long-lived background writer, running concurrently with per-request readers. To avoid two writers contending on SQLite's `BEGIN IMMEDIATE` lock, **all triggers feed a single serialised walk task** (an asyncio queue): a trigger enqueues "walk requested" (coalescing duplicates), and exactly one walk runs at a time. Readers keep their WAL snapshot reads throughout. The triggers:

- **On startup** — the lifespan keeps the existing synchronous `migrate()` before the server accepts traffic (a bad schema *should* block), then kicks off the initial reconcile as a *background task after* the server is live, so `/healthz` readiness isn't gated on walking a growing tree.
- **On change** — a filesystem watch (via `watchfiles`, which uses inotify on Linux) enqueues walks for sub-second freshness, so the single-tab inbox updates on its own. The watch enqueues only for `*.html` changes under the docs-root (`.feedback-archive/` included, so a doc moving there re-indexes as `archived`), ignoring dotfiles and editor temp/swap files.
- **On demand** — an admin endpoint enqueues a reconcile and returns a summary (counts of created / updated / archived / missing / reactivated / errors + duration), for "something looks stale" and for tests. The endpoint returns the summary of a walk that *observes its request* (a per-request future tied to a walk that started at-or-after the enqueue), not merely the next walk to finish — so the caller always sees what their request did.

(No periodic timer — the watch plus on-demand cover freshness; a periodic safety-net can be added later if the watch proves lossy.)

The queue carries a coarse "walk requested" signal, not the identity of what changed, so each event drives a full incremental (mtime/size-gated) re-walk rather than a targeted update. At the current scale (~56 docs) a gated full pass is cheap; a per-path work queue is deliberately *not* built now — a known limitation to revisit only if the store grows large enough to feel it.

**Robustness.** A file with no parseable meta tag — e.g. caught mid-write by a skill — is skipped and logged, and re-picked-up on the next walk once its mtime settles (`watchfiles`' debounce already coalesces the write burst). A walk that errors is caught and logged; unlike a migration error, it must not crash the server.

**Local and read-only.** The walker only reads under the dev-store root and writes the local index; it never mutates a source doc and needs no network, keeping it inside the test harness's no-socket discipline. The dev-store root is a new configuration value (env override, defaulting to `~/.claude/feature-docs`), mirroring how the skeleton handles the DB path.

**What we won't do in Stage 1:** populate `content_html`, extract rich per-doc metadata (checklist state, section IDs — deferred to whoever needs it), run a periodic walk, or touch the MCP bridge. The walker indexes existence and identity, not content.

## Alternatives considered

1. Walk the filesystem on every request (no index)
  Source: design doc §6 motivation
  Simplest, no schema to keep in sync — but slow on
        every inbox/project render and it couples every view to disk layout. The
        design doc rejects it explicitly; the whole point of this feature is the
        index.
2. Hard-delete index rows for vanished files
  Source: review round 1
  Simpler than a

  state, but

  /

  /

  cascade off

  — so a reconcile would silently wipe a
        user's read timestamp the moment

  (the very next
        feature) lands. Rejected: we mark

  instead and
        keep an explicit rebuild as the only thing that truly drops rows.
3. Have feature-skills emit machine-readable feature status
  Source: review round 1
  Instead of the walker scraping

  's
        presentation table, the tracker-writing skills could emit a

  attribute or JSON island the walker reads robustly —
        the durable long-term answer. Deferred: it's a coordinated change in the
        sibling

  repo; for Stage 1 we scrape with a tolerant
        parser and note this as a follow-up (see Design notes).
4. A periodic re-walk as a freshness mechanism
  Source: design doc scope; review round 1
  A timer every N minutes is simple and deterministically
        testable. Dropped for the first cut in favour of the filesystem watch — Nigel
        wants watch behaviour (and its edge cases) surfaced early rather than hidden
        behind a poll. A periodic safety-net can be added later if the watch proves
        lossy.

## Delivery phases

### Phase 1 — Walker core + index (startup & on-demand)

The walker module, migration `0002` (nullable `feature_id`; `status` column; partial unique index on `source_path`), the docs-root config value, and the serialised walk task that all triggers feed. Two modes: incremental upsert and full reconcile (marking vanished docs `missing`, never deleting). Archive handling is folded in here — docs under `.feedback-archive/` index with `status='archived'`. Wired into startup (background-after-readiness) and an on-demand endpoint that returns a walk summary. Emits an `events` row per change; a no-op walk emits none. **Testable:** point the walker at a temp directory tree and assert the right projects/features/documents/events rows appear; mtime/size gating skips unchanged files; a reconcile marks removed files `missing`; a file reappearing at a `missing` path reactivates the row in place (`missing` → `active`, child rows preserved); an archived doc indexes as `archived`; the endpoint's summary counts are correct; and migration `0002` applies cleanly on a schema_version-1 DB, yielding the new shape (nullable `feature_id`, `status` column, partial unique index).

### Phase 2 — Filesystem watch (live freshness)

A `watchfiles`-based watch on the dev-store that enqueues walks on change, giving story 2's sub-second updates, managed by the app lifespan and cancelled cleanly on `SIGTERM`. Brought in early (ahead of tracker parsing) so watch edge cases — debounce, partial writes, missed events — surface as soon as possible. **Testable:** a change under a watched temp tree drives an index update; a mid-write file (no meta tag) is skipped and picked up on settle; a dotfile / `.swp` / non-HTML change does *not* enqueue a walk (the path filter — deterministic, unlike watch timing). (Watch timing itself is best-effort to unit-test; the enqueue → walk path it drives is deterministic.)

### Phase 3 — Tracker status (features.html parsing)

Index `features.html` at the project root as a project-level document (NULL `feature_id`), and parse its In Progress / Available / Done tables to populate `features.status` / `owner` / `notes`. Tolerant parser, logged on miss. **Note:** `inbox-view` (a downstream feature) depends on this — its categories key on `features.status`, which is NULL until this phase lands. **Testable:** a tracker with known rows yields the expected feature statuses; a hand-mangled tracker degrades gracefully rather than crashing the walk.

## Indicative implementation notes

Plan-level detail worth carrying forward; the planner firms these up.

- **Module placement.** A new `storage/walker.py` (DB-facing, alongside `db.py`); the serialised walk task, the watch, and the endpoint wiring live in the `web/` layer. The walker opens its own connection via `connect()`/`transaction()` (it's a writer outside the request cycle), not the per-request `request_conn` helper.
- **Config.** Add `docs_root()` to `config.py` — env override `FEATURE_SKILLS_WEBAPP_DOCS_ROOT`, default `~/.claude/feature-docs`, fail-loud if set-but-missing, mirroring `db_path()`/`port()`.
- **Migration 0002.** Three changes, schema_version → 2: (1) make `documents.feature_id` nullable; (2) add `documents.status` (TEXT, default `'active'`); (3) a partial `CREATE UNIQUE INDEX … ON documents(source_path) WHERE source_path IS NOT NULL`. SQLite can't drop a NOT NULL via `ALTER`, so (1) needs a new table shape. Approach: `DROP TABLE documents; CREATE TABLE documents(…)` with the new shape — safe because `documents` is guaranteed empty when 0002 applies (its only writer is this feature's walker, which ships *with* 0002, so nothing wrote rows at schema_version 1), and the child tables (`read_state`/`synthesis_responses`/`comments`/`events`) are empty too, so the drop is clean and FK-by-name re-resolves after recreate. This avoids the canonical 12-step rebuild, whose `PRAGMA foreign_keys=OFF` can't run inside `migrate()`'s single `BEGIN IMMEDIATE`. If rows ever needed preserving across such a change, use `PRAGMA defer_foreign_keys=ON` (transaction-safe) + `INSERT … SELECT` instead. All plain DDL, so the runner's naive `;`-split is safe.
- **Serialised walk task.** An `asyncio.Queue` (or a single-worker task with a "dirty" flag) that startup, the watch, and the endpoint all post to; coalesce redundant requests. The endpoint awaits its enqueued walk to return the summary.
- **Meta extraction.** Stdlib `html.parser.HTMLParser` for `<meta name="feature-doc-type">` and `<title>`; no `lxml`/`beautifulsoup`. No recognised meta tag → skip (logged), don't index.
- **Event vocabulary.** One set, used consistently by the data-model prose, the endpoint summary, and inbox-view: `created`, `updated`, `archived`, `missing`, `reactivated`; `payload_json` carries path/type/feature for the activity feed.
- **Timestamps.** `created_at`/`updated_at` = index write time (ISO-8601 TEXT, kea convention); `source_mtime` = the file's mtime. Gating compares stored `source_mtime` (+ size) to the file's current values.
- **features.html parsing.** Template-generated with stable section ids (`in-progress`/`available`/`done`) and `td` classes (`feature-name`/`owner`/`notes`) — parse those, but keep it tolerant: a hand-tweaked tracker should degrade (log + skip) rather than crash. This presentation-scraping is acknowledged-fragile; the durable fix is the feature-skills machine-readable-status follow-up.
- **watchfiles dependency.** Add to `pyproject.toml` runtime deps. Watch the docs-root recursively; debounce coalesces a skill's multi-write burst into one walk.
- **Atomic-write follow-up (feature-skills).** The robust fix for mid-write reads is for the doc-writing skills to write to a temp file and `rename()` into place (atomic on POSIX). Out of scope here (sibling repo) but recommended; pairs with the machine-readable-status change.
- **Background-task lifecycle.** Start the walk task and the watch in the lifespan after the synchronous migrate; cancel and await them on shutdown so `SIGTERM` stays clean (the skeleton relies on graceful drain).

## Design notes

- **Nullable `feature_id` (round 1, Nigel).** Chose to make `documents.feature_id` nullable rather than invent a sentinel feature — project-level docs (`features.html`, and plausibly other non-feature docs later) belong to a project, not a feature, and the model should say so honestly.
- **Single `status` column, not hard-delete (round 1).** Consolidated three review points — the design doc's "status archived" wording, the archived-flag routine item, and the mark-don't-delete decision — into one `documents.status` (`active`/`archived`/`missing`). Marking instead of deleting protects the read-state / comments / synthesis rows that cascade off `documents` and that `read-state` (the next feature) starts populating.
- **Startup reconcile runs after readiness (round 1).** Keep `migrate()` blocking before `yield`; run the first walk as a background task after the server is live, so `/healthz` doesn't 503 while a growing tree is walked — preserving the readiness contract `skill-integration-parallel` depends on.
- **watchfiles in, periodic out (round 1, Nigel).** First cut is startup + on-demand + watch, no periodic timer — surface watch edge cases early. A periodic safety-net is a later add if the watch proves lossy.
- **Single serialised writer (round 1).** All triggers feed one walk task so the background writer never contends with itself on `BEGIN IMMEDIATE`; readers stay on WAL snapshots.
- **Tracker scraping is interim (round 1).** Parsing `features.html`'s table is accepted for Stage 1 with a tolerant parser; the durable answer is machine-readable status emitted by feature-skills — recorded as a follow-up, not built here.
- **Migration 0002 = DROP + CREATE, not a rebuild (round 2).** Making `feature_id` nullable would need SQLite's 12-step rebuild, whose `PRAGMA foreign_keys=OFF` can't run inside `migrate()`'s single transaction. Since `documents` is provably empty at migration time (the walker is its first and only writer, shipped with 0002), we drop and recreate the table instead — no runner change. Replaced the earlier, fragile "trivial because empty → rebuild" wording.
- **Reactivation keyed on path (round 2).** A returning file at a `missing` path revives that row in place (→ `active`, `reactivated` event), preserving child rows; a different doc at a reused path inheriting the old identity is accepted in Stage 1 (regenerable index).
- **Endpoint returns its own walk's summary (round 2).** With coalescing + a single walk task, the on-demand endpoint awaits a walk that observes its request, so story 4's "see what it did" can't get a stale summary.
- **Readiness ≠ index populated (round 2).** `/healthz` ok means "server up + schema migrated", not "index populated" — the startup reconcile runs after readiness, so `inbox-view` must render an empty index gracefully during cold boot rather than treating it as an error.
- **Coarse walk signal accepted (round 2).** The queue carries "walk requested", not what changed, so every event re-walks the whole (gated) tree. Cheap at current scale; a per-path queue is deliberately not built now.
- **No drop-all rebuild in Stage 1 (planning).** The on-demand endpoint is a reconcile, not a nuclear rebuild; a full drop-all-and-re-walk is deferred (reconcile, or deleting the regenerable DB file, suffices). Add a rebuild endpoint later only if a need emerges.
- **documents gains `project_id` (planning).** With `feature_id` nullable, a project-level doc can't reach its project via `features`, so migration 0002 also adds a `project_id` (NOT NULL) to `documents`; every doc carries its project directly.

## Review decisions

### Round 1 (post-merge review, all phases on main)

- **events.payload_json now populated.** The walker was leaving `payload_json` NULL despite the plan; every event (created / updated / archived / reactivated / missing) now records `{path, type, feature}`, so the downstream activity feed has its data at the source.
- **Stale in-package docs removed + guarded.** Deleted `feature_skills_webapp/docs/.../requirements.md` (shipped in the wheel) and added `"**/docs/**"` to the wheel `exclude` so docs can't ship again.
- **v1→v2 migration upgrade-path test added.** The plan called for it; tests previously only built fresh DBs. New test applies only `0001`, then `migrate()`, asserting the in-place upgrade to v2.
- **Shutdown lost-wakeup closed.** On `CancelledError` the walk worker now drains the queue and resolves *queued-but-unbatched* futures too (not just the in-flight batch), so a shutdown-racing request can't hang on the 30s timeout. Covered by a new test.
- **Polish.** `features.html` read once (not twice) per changed walk; module-level `dataclasses` import in routes; comment on the dead-but-defensive tracker `try/except`; documented the reactivated-over-archived event precedence; `WalkRequest.future` parameterised as `asyncio.Future[WalkSummary]`.
