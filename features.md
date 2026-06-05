# feature-skills-webapp — Features

## In Progress

| Feature | Owner | Notes |
|---|---|---|
| [inbox-view](docs/features/inbox-view/context.md) | Nigel | The headline UX — Home inbox grouping doc cards by category (awaiting input / new since last visit / in progress / recently shipped), cross-project by default. |

## Available

| Feature | Notes |
|---|---|

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
| [read-state](docs/features/read-state/context.md) | **Shipped.** First writer of the `read_state` table (no migration): a `storage/read_state.py` with `mark_read` (idempotent upsert through `transaction()`), `mark_all_read` (stamps every active doc in a project), and `unread_document_ids` — the inbox's "new since last visit" predicate (active docs with an event newer than `last_read_at`; `COALESCE(…, '')` so a missing row = never-read; strict `>` so ties read as read; optional project filter). A shared `now_iso()` helper in `db.py`, adopted by the walker too, guarantees `last_read_at` and `events.created_at` stay byte-comparable. The bulk action is exposed as name-keyed `POST /admin/projects/{project}/mark-read` (404/503 guards); the per-render stamp is deferred to `doc-view`. Two phase PRs + one post-merge review-fix round (equal-timestamp tie test); 101 tests, ruff/ty clean. |
| [doc-discovery](docs/features/doc-discovery/context.md) | **Shipped.** Filesystem walker indexing the dev-store into the §4 SQLite schema: migration 0002 (DROP+recreate `documents` with `project_id`, nullable `feature_id`, a `status` column — active/archived/missing — and a partial unique index on `source_path`); a synchronous mtime+size-gated `walk()` with status transitions and an `events` row (carrying a `payload_json`) per change; a single serialised async walk worker fed by startup reconcile, an on-demand `POST /admin/discover`, and a `watchfiles` watch; plus `features.html` tracker parsing into `features.status`/`owner`/`notes`. Three phase PRs + one post-merge review-fix round; 85 tests, ruff/ty clean, CI added. |
| [webapp-skeleton](docs/features/webapp-skeleton/context.md) | **Shipped.** Supervised Starlette server on `127.0.0.1:8800` with a Jinja placeholder page and a DB-backed `/healthz` readiness check; migrated SQLite carrying the full §4 schema (per-request connections, WAL, `events` SET-NULL audit semantics, FK indexes); systemd user unit with a working crash-loop cap; kea-style test harness (xdist + pytest-socket, per-worker DB). Three phase PRs + one review-fix round; 27 tests, ruff/ty clean. |
