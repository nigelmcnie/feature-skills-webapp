# feature-skills-webapp — Features

## In Progress

| Feature | Owner | Notes |
|---|---|---|

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
| [webapp-skeleton](docs/features/webapp-skeleton/context.md) | **Shipped.** Supervised Starlette server on `127.0.0.1:8800` with a Jinja placeholder page and a DB-backed `/healthz` readiness check; migrated SQLite carrying the full §4 schema (per-request connections, WAL, `events` SET-NULL audit semantics, FK indexes); systemd user unit with a working crash-loop cap; kea-style test harness (xdist + pytest-socket, per-worker DB). Three phase PRs + one review-fix round; 27 tests, ruff/ty clean. |
