# feature-skills-webapp — Features

## In Progress

| Feature | Owner | Notes |
|---|---|---|

## Available

| Feature | Notes |
|---|---|
| [read-state](docs/features/read-state/context.md) | Stamp `last_read_at` per document on view; drives the inbox's "new since last visit". Next in the build order (deps shipped). |

## Suggested order

Topological build order per the design doc (§6).

1. `webapp-skeleton` — bones first; everything depends on it
2. `doc-discovery` — filesystem walker + SQLite index
3. `read-state` — stamp last_read_at on every view
4. `inbox-view` — the headline UX
5. `doc-view` — render individual docs in the webapp shell
6. `sse-refresh` — live inbox updates via SSE
7. `synthesis-response-capture` — POST instead of clipboard
8. `comment-capture` — persistent click-to-comment
9. `skill-integration-parallel` — drop Chrome calls from feature-skills

## Done

Completed or obsoleted; kept here for context.

| Feature | Outcome |
|---|---|
| [doc-discovery](docs/features/doc-discovery/context.md) | **Shipped.** Filesystem walker indexing the dev-store into the §4 SQLite schema: migration 0002 (DROP+recreate `documents` with `project_id`, nullable `feature_id`, a `status` column — active/archived/missing — and a partial unique index on `source_path`); a synchronous mtime+size-gated `walk()` with status transitions and an `events` row (carrying a `payload_json`) per change; a single serialised async walk worker fed by startup reconcile, an on-demand `POST /admin/discover`, and a `watchfiles` watch; plus `features.html` tracker parsing into `features.status`/`owner`/`notes`. Three phase PRs + one post-merge review-fix round; 85 tests, ruff/ty clean, CI added. |
| [webapp-skeleton](docs/features/webapp-skeleton/context.md) | **Shipped.** Supervised Starlette server on `127.0.0.1:8800` with a Jinja placeholder page and a DB-backed `/healthz` readiness check; migrated SQLite carrying the full §4 schema (per-request connections, WAL, `events` SET-NULL audit semantics, FK indexes); systemd user unit with a working crash-loop cap; kea-style test harness (xdist + pytest-socket, per-worker DB). Three phase PRs + one review-fix round; 27 tests, ruff/ty clean. |
