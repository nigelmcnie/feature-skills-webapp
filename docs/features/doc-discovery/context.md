# doc-discovery

## Problem space and motivation

`webapp-skeleton` shipped a supervised Starlette server with a migrated SQLite database carrying the full §4 schema — but every table is empty. `doc-discovery` is the feature that first puts data in it: it walks the dev-store and indexes what's there. It's the second feature in the build order, and everything user-facing (`read-state`, `inbox-view`, `doc-view`) reads the index it produces.

The webapp needs a *model* of what docs exist. Today the dev-store at `~/.claude/feature-docs/<project>/<feature>/` is just a growing pile of static HTML — at the time of writing roughly 14 context, 19 requirements, 20 plan and 3 `features.html` docs across the `kea`, `planning` and `feature-skills-webapp` projects — discoverable only by `grep` or Chrome history. Walking the filesystem on every request would be slow; an index makes the inbox and project views cheap to render.

This is firmly a **Stage 1 (parallel mode)** feature. The dev-store files stay canonical; the walker re-discovers them from disk and the SQLite rows are a derived, regenerable index. At Stage 2 — when SQLite holds canonical doc content and skills write via MCP — the walker is retired. So the design should treat the index as disposable: rebuildable from disk at any time, never the source of truth.

## Related work

- **`webapp-skeleton` (shipped).** Laid the full §4 schema in `0001_init.sql`. `doc-discovery` is the first writer to `projects`, `features`, `documents` and `events`. The `documents` table already has exactly the columns the walker fills: `type`, `source_path`, `metadata_json`, `source_mtime`. See its [context](../webapp-skeleton/context.html), [requirements](../webapp-skeleton/requirements.html) and [plan](../webapp-skeleton/plan.html).
- **The dev-store HTML is already lens-friendly.** The earlier HTML migration deliberately stamped every doc with `<meta name="feature-doc-type" content="…">` so a webapp could be a thin lens over it without another data-shape change. The walker keys on that tag rather than on filenames. `features.html` carries `content="features"` too.
- **kea storage conventions.** The skeleton ported kea's `connect()`/`transaction()`/per-request-connection pattern; the walker's writes should go through the same `transaction()` helper (autocommit + explicit `BEGIN IMMEDIATE`), not bare `with conn:`.
- **The `events` table is the spine the inbox derives from.** The design doc describes the inbox as "docs with events newer than `last_read_at`". So emitting an `events` row per discovered change isn't incidental bookkeeping — it's the data the next two features query.

## Constraints and considerations

- **Identify by meta tag, not filename.** Read `<meta name="feature-doc-type">` from each `.html`. Values seen in the live store: `context`, `requirements`, `plan`, `features`, plus synthesis variants like `requirements-feedback-1`.
- **Path → project + feature slug.** The structure is `<project>/<feature>/<doc>.html`. But `features.html` sits one level up at `<project>/features.html` — no feature slug. The design doc calls this out: *track `features.html` as its own doc-type, separately from per-feature docs.*
- **`.feedback-archive/` subdirs.** These hold archived synthesis/feedback docs (e.g. `requirements-feedback-1.html`). Per the design doc, index them with `status archived` — visible in history, kept out of the live inbox.
- **`source_mtime` gating.** Track mtime per doc and only re-parse on change. Upsert keyed on `source_path` so a re-walk is idempotent.
- **Triggers, in order of ambition.** Startup, periodic (every N minutes), an on-demand admin endpoint, and a filesystem watch via `inotify`/`watchfiles` for sub-second freshness. Linux-only is explicitly fine.
- **Emit an `events` row per discovered change** (created / updated / archived) so the activity feed and the later SSE push have something to consume.
- **No-network test discipline.** The skeleton's harness runs under `pytest-socket` with sockets disabled and a per-worker DB. The walker must be exercisable against a temp directory tree + temp DB; don't introduce anything that needs the network at test time.

## Links

- Design doc: [feature-skills webapp design](file:///home/nigel/src/nigelmcnie/feature-skills/docs/webapp.html) — §6 `doc-discovery` feature card, §4 data model, §5 migration path
- Depends on: [webapp-skeleton](../webapp-skeleton/context.html) (shipped — server, schema, supervision)

## Open questions

1. Does the walker **own creating `projects` and `features` rows** from the path, or only `documents`? `features.status`/`owner`/`notes` don't come from the per-doc path — they live in the tracker. So is the feature row created bare on first sighting of a doc, with the tracker fields filled in later?
2. Relatedly — does `doc-discovery` **parse `features.html`'s tracker tables** (In Progress / Available / Done) to populate `features.status`, or does it just index the file as one `features`-typed document and leave status-derivation to a later feature?
3. **Deletion handling.** When a doc disappears from disk, do we delete the `documents` row or soft-retire it? The schema set `events.document_id` to `ON DELETE SET NULL` specifically so audit history survives a deleted document — the walker's deletion path should respect that intent rather than cascade-wiping history.
4. **inotify vs poll on a network mount.** The design doc raises this and answers it: the dev-store is almost certainly local, so punt — but worth a deliberate decision rather than an accident.
