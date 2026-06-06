# feature-skills-webapp — Features

## In Progress

| Feature | Owner | Notes |
|---|---|---|
| [sse-refresh](docs/features/sse-refresh/requirements.md) | Nigel | Live inbox updates via SSE — `/events` endpoint pushes a change signal; the open tab re-fetches and swaps the inbox region without a manual reload. |

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
| [doc-view](docs/features/doc-view/requirements.md) | **Shipped.** Renders an individual doc inside the webapp shell, closing the inbox loop inbox-view/read-state left open. A new `web/doc_view.py` with two id-keyed routes: `/doc/{id}` (a thin Jinja shell — breadcrumbs from one `documents`→`projects` LEFT-JOIN-`features` lookup, "View source" hatch, read-state stamped once on shell render via the existing `mark_read`) and `/doc/{id}/raw` (serves the indexed file's bytes — id-lookup only, no path-traversal surface, prefers `content_html` as the Stage-2 seam). The doc renders verbatim in a no-sandbox, full-height internal-scroll `<iframe>` so its own CSS/JS (TOC, highlighting, click-to-comment) run intact — the iframe-passthrough choice over inlining (deferred to Stage 2) and document_id routing over readable URLs. Inbox cards with a `document_id` became accessible `aria-label`'d links; graceful 404 / "no longer available" (missing) / 503 states; tracker (`project / Tracker`) and archived docs render but aren't surfaced. Phase 2 added sibling prev/next nav across a feature's active docs ordered by a single `DOC_TYPE_ORDER`. Two follow-up fixes for back-button stale read-state (`Cache-Control: no-store` + bfcache `pageshow` reload). Two phase PRs + one post-merge review-fix round (fold feature-id into the join, no-store test, dedup the Tracker literal); 156 tests, ruff/ty clean. |
| [inbox-view](docs/features/inbox-view/context.md) | **Shipped.** The headline UX — a cross-project inbox Home page replacing the placeholder at `/`. A read-model layer (`storage/inbox.py`: connection-taking `new_since_last_visit` / `in_progress` / `recently_shipped` + `build_inbox`, mirroring `read_state.py`) derives three categories: New since last visit (the unread predicate, INNER-JOINed to features so the null-feature tracker doc and missing/archived docs drop out, uncapped, newest-first), In progress (`features.status='in_progress'`), and Recently shipped (capped+30-day-windowed). Ship time comes from a new `shipped` event the walker emits on the done-transition (`document_id=NULL`, `{project,slug}` payload — matching the design's anticipated sse "ship" event) rather than the unreliable `features.updated_at`. Server-rendered Jinja, no HTMX; not-configured / all-empty / populated states (`Inbox.is_empty`); per-project filter chips; humanised type labels; display-only cards (click-through + read-stamp deferred to `doc-view`, "Awaiting your input" deferred to `synthesis-response-capture`). Three phase PRs + one post-merge review-fix round (empty-state branch collapse, deterministic tie-ordering); 134 tests, ruff/ty clean. |
| [read-state](docs/features/read-state/context.md) | **Shipped.** First writer of the `read_state` table (no migration): a `storage/read_state.py` with `mark_read` (idempotent upsert through `transaction()`), `mark_all_read` (stamps every active doc in a project), and `unread_document_ids` — the inbox's "new since last visit" predicate (active docs with an event newer than `last_read_at`; `COALESCE(…, '')` so a missing row = never-read; strict `>` so ties read as read; optional project filter). A shared `now_iso()` helper in `db.py`, adopted by the walker too, guarantees `last_read_at` and `events.created_at` stay byte-comparable. The bulk action is exposed as name-keyed `POST /admin/projects/{project}/mark-read` (404/503 guards); the per-render stamp is deferred to `doc-view`. Two phase PRs + one post-merge review-fix round (equal-timestamp tie test); 101 tests, ruff/ty clean. |
| [doc-discovery](docs/features/doc-discovery/context.md) | **Shipped.** Filesystem walker indexing the dev-store into the §4 SQLite schema: migration 0002 (DROP+recreate `documents` with `project_id`, nullable `feature_id`, a `status` column — active/archived/missing — and a partial unique index on `source_path`); a synchronous mtime+size-gated `walk()` with status transitions and an `events` row (carrying a `payload_json`) per change; a single serialised async walk worker fed by startup reconcile, an on-demand `POST /admin/discover`, and a `watchfiles` watch; plus `features.html` tracker parsing into `features.status`/`owner`/`notes`. Three phase PRs + one post-merge review-fix round; 85 tests, ruff/ty clean, CI added. |
| [webapp-skeleton](docs/features/webapp-skeleton/context.md) | **Shipped.** Supervised Starlette server on `127.0.0.1:8800` with a Jinja placeholder page and a DB-backed `/healthz` readiness check; migrated SQLite carrying the full §4 schema (per-request connections, WAL, `events` SET-NULL audit semantics, FK indexes); systemd user unit with a working crash-loop cap; kea-style test harness (xdist + pytest-socket, per-worker DB). Three phase PRs + one review-fix round; 27 tests, ruff/ty clean. |
