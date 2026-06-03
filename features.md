# feature-skills-webapp — Features

## In Progress

| Feature | Owner | Notes |
|---|---|---|

## Available

| Feature | Notes |
|---|---|
| [webapp-skeleton](docs/features/webapp-skeleton/context.md) | Starlette app, SQLite schema, systemd unit — the foundation everything else builds on. |

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
