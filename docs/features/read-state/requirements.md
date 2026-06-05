# read-state

## Problem

`doc-discovery` shipped the index and an append-only `events` log: every time a doc is created, updated, archived, marked missing, or reactivated, a row lands in `events` with a UTC timestamp. That's half of what the inbox needs. The headline view — "New since last visit" — is defined as *documents with events newer than I last read them*. But nothing records when I last read a doc. The `read_state(document_id, last_read_at)` table was created empty in `webapp-skeleton`'s `0001_init.sql` and has had no writer since.

Without it, every doc with any event looks perpetually "new", so the inbox can't distinguish a doc the agent regenerated overnight from one I read an hour ago. `read-state` is the writer for that table and the owner of the "is this unread?" query. It's small, but it's the last piece of plumbing before the first user-facing view: both `inbox-view` and `doc-view` list it as a dependency.

**Scope boundary.** This feature powers the inbox's "New since last visit" category only. The inbox's other attention category, "Awaiting your input", derives from `synthesis_responses` and is untouched here.

## Vision

The webapp remembers which docs you've seen: opening a doc marks it read, so the inbox can answer "what's new since I last looked?" as a single cheap query against the index.

## User stories

1. As the inbox-view author (next feature)
  I want one query that returns which documents are
        unread — those with an event newer than their last-read time — so the
        "New since last visit" category is a single SQL query, not per-doc
        bookkeeping.
  Building the Home page, I call an "unread docs"
        query (cross-project, or filtered to one project) and render the
        result straight into cards. I never touch

  row
        shapes directly.
2. As the developer using the webapp
  I want opening a doc to mark it read, so it stops
        being flagged "new" the next time I look at the inbox.
  I read a requirements doc in the morning. That
        afternoon the inbox no longer flags it — but a fresh

  doc the agent wrote at lunchtime

  show, because its event is newer than my morning read.
3. As the developer after a burst of agent activity
  I want a single "mark everything in this project
        read" action, so I can clear the slate without opening every doc.
  An overnight routine regenerated eight docs
        across a project. I skim the two I care about; for the rest I hit
        "mark all read" and the project drops out of "New since last visit".

## Data model

No new tables and **no migration** — `read_state` already exists from `0001_init.sql`: `read_state(document_id, last_read_at)`, with `document_id` as the primary key referencing `documents(id) ON DELETE CASCADE`. One row per document; the row is created lazily on first read and updated in place thereafter.

Key relationships and rules:

- **One row per document, single user.** No user dimension — the design fixes this as a single-user tool, so the row is keyed by document alone (this is why `document_id` is the PK, not part of a composite).
- **"Unread" is a comparison against `events`.** A document is unread when it has an event whose timestamp is newer than its `last_read_at`. *Any* event type (created / updated / archived / missing / reactivated) counts — the comparison is purely on timestamp. Equivalently, a doc is unread iff its *newest* event is newer than `last_read_at`: the design doc's "max event" phrasing and this "any newer event" phrasing are the same condition. Archived/missing docs are kept out of the inbox by filtering on the *document's* status (below), not by filtering which event types count.
- **A missing row means never read.** A document with no `read_state` row has never been read, so every event counts as new — it is unread by definition. Consumers must treat a missing row as "read at the beginning of time", never as an error.
- **Only `active` documents participate.** The unread calculation considers documents with `status = 'active'` only; `archived` and `missing` docs stay in history but never surface as "new since last visit", matching `doc-discovery`'s intent that those are kept out of live views.
- **Ties resolve as read.** The comparison is strict (`event.created_at > last_read_at`), so an event landing at the exact same instant as a read reads as *already read*. This is the deliberate bias: don't re-flag a doc the instant after you read it.
- **Timestamps must be byte-for-byte comparable (correctness requirement).** `last_read_at` must be written in the *identical* textual format `doc-discovery` uses for `events.created_at` — UTC, ISO-8601, microsecond precision, explicit `+00:00` offset — so the "newer than" test is a valid lexicographic string comparison. A divergent format (a bare `Z` suffix, a naive no-offset timestamp, or second precision) sorts incorrectly and silently breaks the unread test. The implementation must therefore have a single source of truth for this timestamp format shared with the walker (see Technical approach), not two independently-maintained expressions.
- **Reading is not an audited change.** Marking a doc read writes only the `read_state` row. It does *not* emit an `events` row — `events` is the log of what happened to a document's *content*, not a log of the user's attention.
- **Reactivation keeps read state, and that's safe.** Because `doc-discovery` marks vanished docs `missing` rather than deleting them, the cascade never fires and the `read_state` row survives a missing→reactivated round-trip. `doc-discovery` flagged the sharp case as read-state's concern: a *different* doc landing at a reused `source_path` reactivates the existing row in place, inheriting the prior `last_read_at`. This is harmless — the reactivation walk emits a `created`/`reactivated` event stamped at walk time (the newest timestamp in the system), which is strictly greater than any inherited `last_read_at`, so the doc correctly surfaces as new.

## Technical approach

Build `read-state` as a thin set of storage-layer operations plus one admin endpoint — no new schema, no new abstractions, reusing the existing `transaction()` / per-request-connection patterns the walker already established.

### Three storage operations

- **Mark one document read.** Upsert the document's `read_state` row to "now". This is the operation `doc-view` will call when it renders a doc.
- **Mark all read for a project.** Upsert "now" for every `active` document in a project in one transaction. Non-active (archived/missing) docs are deliberately left untouched, consistent with the active-only unread rule. Backs the bulk admin action.
- **Query unread documents.** Return the documents that are `active` and have at least one event newer than their `last_read_at` (treating a missing row as never-read). A single function with an *optional* project filter serves both the per-project and cross-project cases. This is the exact predicate `inbox-view` needs; `read-state` owns and tests it, while `inbox-view` shapes the final SELECT columns.

### A single source of truth for the timestamp format

To guarantee the comparability requirement above, `read-state` and the walker must produce their timestamps from *one shared helper*, so the two formats cannot drift apart over time. Concretely this means the walker's currently-inlined "now" expression and read-state's `last_read_at` both come from the same function. This is a small, deliberate touch to the shipped walker module — accepted as the durable fix for the single highest-risk correctness item in this feature.

### Where the per-render stamp lives

The design doc frames the stamp as happening "on every doc-view request… on render, not on close". But `doc-view` comes *after* `read-state` in the build order, so the actual render-time call is wired in when `doc-view` lands. `read-state`'s job is to *provide and fully test* the mark-read operation now; `doc-view` calls it server-side inside its render handler (no client-side beacon, no separate "I read this" endpoint). The one piece `read-state` exposes over HTTP now is the bulk "mark all read" admin action, because that's a self-contained, end-to-end-testable user action that doesn't depend on `doc-view` existing.

### The bulk admin endpoint

`POST /admin/projects/<project>/mark-read`, with the project identified by **name** (`projects.name` is `UNIQUE`, and it matches the design doc's name-keyed routes such as `/<project>/<feature>/<doc-type>`). It returns a small JSON summary in the style of the existing `/admin/discover` endpoint — the count of active documents stamped. An unknown project name returns 404.

### What stays out of scope

No per-user state, no read/unread *boolean* (a timestamp is required — see alternatives), no scroll- or dwell-tracking, no page-close beacon, and no automatic render-time wiring (that ships with `doc-view`). And, per the scope boundary in Problem, nothing here touches the "Awaiting your input" / `synthesis_responses` path.

## Alternatives considered

1. Store a read/unread boolean instead of a timestamp
  Source: design doc data model (§4)
  A boolean can't express "new

  I
        last looked". A doc I read this morning that the agent edits this
        afternoon must re-surface as new; only a timestamp compared against
        the event log captures that. The schema already commits to

  for this reason.
2. A dedicated single-document "mark read" HTTP endpoint
  Source: build-order analysis (read-state precedes doc-view)
  Tempting as a way to exercise the write path
        before

  exists, but

  stamps
        server-side during render — a separate client POST would duplicate
        that and reintroduce "on render vs on close" timing questions the
        design doc explicitly closed. The storage operation is tested
        directly instead; the bulk admin endpoint covers the HTTP path.
3. Bulk action stamps each doc to its own latest event time
  Source: review round 1
  Instead of wall-clock

  , "mark
        all read" could set each doc's

  to its newest
        event's timestamp. Rejected as needless complexity:

  is later than every existing event, so the effect (the project clears
        from "New since last visit") is identical, and a doc that gains an
        event after the bulk action re-surfaces correctly either way.
4. Stamp on page close (unload beacon)
  Source: design doc, read-state card (§6)
  Rejected by the design: "closer-to-correct is
        the enemy of perfect-and-complex." Opening a doc marks it read, full
        stop.
5. Per-user read state / user table
  Source: design doc, read-state card (§6)
  The webapp is single-user by design; a user
        table is dead weight. Read state is keyed by document alone.

## Delivery phases

### Phase 1 — Read-state storage operations + unread query

The core, with no HTTP surface. Mark-one-read (upsert) and the unread query (active docs with an event newer than `last_read_at`, missing-row = never-read, with an optional project filter), both built on a timestamp drawn from the shared now-helper. Fully unit-tested against a temp DB: marking a doc read flips it out of "unread"; a later event flips it back in; a doc with no `read_state` row reads as unread; `archived` / `missing` docs are excluded; and calling mark-read repeatedly (as a render path would) is idempotent — one row, cheaply. This phase is what unblocks `inbox-view`.

### Phase 2 — Bulk "mark all read" admin action

Mark-all-read-for-a-project storage operation plus the `POST /admin/projects/<project>/mark-read` endpoint that drives it (project keyed by name), returning a JSON summary — the count of active documents stamped — in the style of `/admin/discover`. End-to-end tested: the endpoint clears a project's unread set; the summary count is accurate; an unknown project name returns 404.

## Indicative implementation notes

Plan-level detail worth carrying forward (not requirements constraints):

- **Shared timestamp helper.** The walker currently inlines `datetime.now(tz=UTC).isoformat()` (`walker.py:358`). Extract a single helper (e.g. a module-level `_now()` returning that exact value) and have both the walker and read-state call it. This realises the comparability requirement from the data model — one expression, no drift.
- **Upsert shape.** `INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?) ON CONFLICT(document_id) DO UPDATE SET last_read_at = excluded.last_read_at`, inside `transaction()` (BEGIN IMMEDIATE).
- **Unread predicate — two equally valid forms.** The "missing row = never-read" contract can be implemented either as `COALESCE(r.last_read_at, '')` (`''` sorts before any ISO timestamp) or as a `LEFT JOIN read_state r … WHERE r.last_read_at IS NULL OR e.created_at > r.last_read_at`. The contract is the requirement; the SQL form is a plan choice. Roughly: `documents d` where `d.status='active'` and an `EXISTS` over `events` satisfies the above. `inbox-view` shapes the final columns.
- **Module placement.** A `read_state.py` in the `storage` layer (sibling to `walker.py`), with the admin endpoint wired into `web/routes.py` and registered in `web/app.py` alongside `admin_discover`.
- **Indexing.** `idx_events_document` on `events(document_id)` already exists. Defer any new index until profiling justifies it at the dev-store's current scale (tens of docs). If/when it's needed, note that because the predicate filters `status='active'` first, a partial index on `documents(status)` may matter as much as an `events(document_id, created_at)` composite. Related implicit coupling: the active-only filter ties read-state to `doc-discovery`'s `status` semantics — if a future change surfaces archived docs in some view, the unread predicate won't include them without a change here.
- **Test discipline.** Inherit the skeleton's harness — `pytest-socket` with sockets disabled, xdist with a per-worker temp DB. Seed `documents` + `events` rows directly, then assert the operations. The endpoint test follows `routes_test.py` / `discovery_test.py` patterns.

## Design notes

- **Timestamp format is a correctness requirement, backed by a shared helper (round 1).** Rated the highest-risk item in the feature. Chose a single shared now-helper (touching the shipped walker) over documenting-and-duplicating the format, because duplication can silently drift and break the unread comparison.
- **Bulk endpoint is name-keyed (round 1).** `POST /admin/projects/<project>/mark-read`; project by name to match the design's name-keyed routes; summary reports the count of active docs stamped (rather than only those that were previously unread) for simplicity.
- **Accepted integration gap (round 1).** The single-doc mark-read op isn't exercised end-to-end by a real user action until `doc-view` lands; it's covered by direct storage-layer unit tests in the meantime. Accepted because inventing a throwaway single-doc endpoint now would be obsoleted by `doc-view` immediately.
- **Reused-`source_path` reactivation is safe (round 1).** An inherited `read_state` row is harmless because reactivation always emits a fresh, newer event — worked through in the data model.

## Review decisions

### Round 1 (post-merge review, both phases on main)

- **Added an equal-timestamp tie regression test.** The "ties resolve as read" contract (strict `>`) was correct in the SQL but had no direct test. Added `test_equal_timestamp_tie_reads_as_read`: an event at exactly `last_read_at` stays read; one microsecond later re-flags. Locks the behaviour against a future operator change.
- **Fixed a stale module docstring.** `storage/read_state.py`'s docstring listed only `mark_read`/`unread_document_ids`; added `mark_all_read` (phase 2). Trivial.
- **No other changes.** The reviewer cleared the in-handler lazy imports (house style), the post-commit `len(rows)` count, the active-docs `stamped` count semantics, injection safety (parameterised lookup), and the unauthenticated admin surface (existing project posture) — no action needed.
