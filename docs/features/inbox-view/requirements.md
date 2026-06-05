# inbox-view

## Problem

The webapp can now *see* the dev-store but can't *show* it. `webapp-skeleton` stood up the server, `doc-discovery` walks `~/.claude/feature-docs/` into the `documents` / `events` index, and `read-state` records when each doc was last read — yet the only route that renders anything is `/`, which still serves the "Placeholder — coming soon" page from `webapp-skeleton`. Every piece of plumbing for the headline feature is in place and nothing surfaces it.

That leaves the original pain unaddressed. The design doc's first problem — *"What do I need to look at?" has no answer* — is exactly the gap. The dev-store grows: re-rendered requirements, fresh plans the agent wrote overnight, features mid-flight, things recently shipped. Today the only ways to find out what moved are to scroll Chrome history or `grep` the dev-store. There's no single place that answers "what's new, what am I in the middle of, and what just landed" — across every project at once, not one tracker at a time.

Until this view exists, the webapp is — in the design doc's own words — "just a static-file server with extra steps."

## Vision

Opening the always-on webapp tab lands you on a cross-project inbox that groups every feature doc by what it needs from you — new since you last looked, in flight, or just shipped — so a glance answers "what should I look at?" without grepping the dev-store.

## User stories

1. As Nigel, returning after the agent worked overnight
  I want a "New since last visit" group listing docs that changed since I last read them, newest first
  Overnight the agent re-rendered the requirements for two features and wrote a fresh plan for a third. I open the tab in the morning
        and all three appear as cards under "New since last visit", most-recently-changed at the top — I don't have to remember what I asked for or trawl Chrome
        history to find them.
2. As Nigel, working across several projects
  I want the inbox to show every project at once by default, with chips to narrow to one
  I have features in flight in both

  and another project. The Home view shows both projects' activity
        interleaved by recency; when I want to focus, I click a project chip and the inbox narrows to just that project.
3. As Nigel, picking work back up after a break
  I want an "In progress" group showing claimed features even when nothing changed today
  itself is claimed and mid-build. No new doc landed this afternoon, but I still want it visible so I keep
        context on what I'm in the middle of — independent of whether its docs have new events.
4. As Nigel, wanting a sense of momentum
  I want a "Recently shipped" group showing the last few features that reached done
  shipped recently; it shows under "Recently shipped" so I can see what just completed without opening the
        tracker. Features that shipped long ago drop off so the group stays a recent-activity feed, not a full archive.
5. As Nigel, on a quiet day with nothing pending — or before the DB is populated
  I want a clear state rather than a blank or broken-looking page
  Everything's read, nothing's in progress, nothing shipped lately — or discovery hasn't run yet. Instead of three empty headings or a
        server error, the inbox tells me "Nothing's waiting for you" (or, if the DB isn't configured, that it has no data yet) and points me at the project views.

## Inbox categories

The design doc names four inbox categories. This feature ships **three**; the fourth is deferred for a concrete data reason (see Alternatives and Design notes). Within every category, cards are ordered **most-recent-activity first**, and the cross-project view interleaves all of a category's cards by recency rather than grouping by project. Each card shows a **humanised doc/feature label** (e.g. "Requirements", "Plan", "Context") rather than a raw type string like `requirements-feedback-1`.

### New since last visit

Documents whose latest activity is newer than when they were last read — the "what moved while I was away" feed. This is the `read-state` unread predicate (active docs with an `events` row newer than `last_read_at`; a doc with no read row counts as fully unread), surfaced as cards. Doc-level granularity: **one card per changed document**, so a single feature can legitimately contribute several cards in one visit (its context, requirements and plan all newer than last read) — that's intended, not duplication. The feed is **uncapped**: every unread doc is listed. Two classes of document are excluded: non-`active` docs (`missing` / `archived`) are already filtered out by the unread predicate, and documents with no owning feature — notably each project's `features.html` tracker, which the walker indexes with a null `feature_id` — are filtered out too, since a card is built around a feature.

### In progress

Features the tracker marks as in progress, shown regardless of whether their docs have new events — so a feature you're mid-build on stays visible on a quiet day. Feature-level granularity: one card per in-progress feature, ordered by the feature's last activity (newest first).

### Recently shipped

The last handful of features that reached done, as a recent-activity feed rather than a full archive: capped to a small count and to a recency window (the design doc suggests "last 5, last 30 days") so stale completions drop off. Ordered by ship time, newest first. "Ship time" is the moment the feature transitioned to done — captured by a dedicated event the walker emits on that transition, not inferred from the feature row's last-touched timestamp (see Data model and Design notes for why).

### Awaiting your input — deferred

The design doc's first category — synthesis docs with an item that has neither a response nor a routine flag — is **out of scope for this feature**. Two facts block it today: synthesis/feedback docs carry no `feature-doc-type` meta tag, so `doc-discovery`'s walker skips them entirely (they never enter the `documents` index); and the category's definition is keyed on `synthesis_responses` rows, a table only written by the later `synthesis-response-capture` feature. That feature is where synthesis state is actually modelled in SQLite, so "Awaiting your input" belongs with it, not here. (Decided with Nigel — see Alternatives.)

### Overlap between categories

The categories are not mutually exclusive and are not de-duplicated against each other. "New since last visit" is doc-level; "In progress" and "Recently shipped" are feature-level. A feature can legitimately appear under "In progress" while one of its changed docs appears under "New since last visit" — the design doc explicitly endorses this ("show it in both"). Each category answers a different question, so a card showing up in two of them is information, not a bug. The design doc's further "cross-references" idea (linking the two appearances) is **out of scope for this feature** — we keep show-in-both, without cross-reference wiring.

## Data model

No new tables and no schema change. `inbox-view` is purely a read/derive feature over the schema `webapp-skeleton` laid down and `doc-discovery` / `read-state` populate:

- **New since last visit** derives from `documents`, `events` and `read_state` — the same comparison `read-state` already ships as its unread predicate, restricted to active docs that belong to a feature.
- **In progress** and **Recently shipped** derive from `features.status` (the tracker-parsed `in_progress` / `done` values), joined to `projects` for the project name.
- Each card's **"last activity" timestamp** comes from the most recent related `events` row — for the unread feed, the document's own latest event; for the feature-level groups, the most recent event across the feature's **active** documents only (consistent with how the unread feed scopes), so a stale `missing`/`archived` event doesn't drive the ordering.

"Recently shipped" needs a notion of *when* a feature shipped, and the obvious proxy — the feature row's `updated_at` — does not work: the walker bumps `updated_at` on *every* tracker upsert, unconditionally, whether or not status changed, so any re-walk past the mtime gate (an owner edit, a notes tweak) would reorder the group and could resurface an old feature inside the recency window. Instead, the walker should emit a dedicated **status-transition event** when a feature flips to `done`; "Recently shipped" reads that event's timestamp for both ordering and the recency cutoff. This is a small addition to `doc-discovery`'s tracker-diff path that this feature depends on (see Delivery phases).

## Technical approach

**A new server-rendered Home page at `/`, replacing the placeholder.** Same Starlette + Jinja2 server-side rendering the rest of the stack uses — no SPA, no build step, and no HTMX yet. The page renders fully on each request; live auto-refresh is a separate, later feature (`sse-refresh`), and pulling it in here would be premature.

**A read-model layer in `storage/`, in the same shape as `read-state`.** Plain functions that take a connection and return the category lists as display-ready rows (project, feature, humanised label, last-activity timestamp), reusing the existing unread predicate rather than re-deriving it. Keeping the queries as testable storage functions — separate from the route — mirrors how `read_state.py` is structured and keeps the inbox's hot path covered by unit tests.

**Cross-project by default; filterable by project.** The unfiltered inbox spans all projects. A project filter narrows every category to one project, surfaced in the UI as chips, "show all" being the default. The same project-scoping the read-state helper already supports carries through. (The exact request shape — e.g. a query parameter — is a plan detail; the requirement is "filter by project".)

**Degrade cleanly when there's no data.** The inbox must never 500 because the database isn't configured or discovery hasn't run. When `db_path` is unset, `/` shows a "not configured / no data yet" state; when the DB is present but every category is empty, it shows the single "Nothing's waiting for you" empty state. A category with no rows is hidden entirely (no empty heading); the all-empty state appears only when all three are empty.

**Cards are display-only in this feature.** Per the decision with Nigel, opening a doc in place — rendering it inside the webapp shell with breadcrumbs and stamping `read_state.last_read_at` — is `doc-view`'s job (the next feature). `inbox-view` renders the card's identity and last activity but does not own a doc route or the read stamp. This keeps the two features cleanly separated and each MR small; the cost is that the inbox isn't click-through-usable until `doc-view` lands immediately after.

**Visual language reuses the existing dark theme.** The inbox adopts the same palette and card styling as the dev-store doc templates so it feels of-a-piece, rendered through the webapp's own Jinja templates (not by passing through any source-file CSS).

## Alternatives considered

1. Ship "Awaiting your input" now
  Source: decided with Nigel
  Rejected for this feature. It has no data to derive from today (synthesis docs aren't indexed, and

  is unwritten until

  ). The options were to add a

  tag to the feedback template (a
        cross-repo change in

  , for transient docs that get archived) or render an always-empty placeholder section — both judged worse
        than simply deferring the category to the feature that models synthesis state. inbox-view ships the three categories that have real data now.
2. Stop-gap click-through (serve raw source HTML from inbox-view)
  Source: decided with Nigel
  Rejected in favour of deferring clickability to

  . A passthrough route would make cards live immediately but
        would duplicate work

  does properly (shell, breadcrumbs, the read-state stamp) and blur the feature boundary. Since

  is the very next feature, the short window where the inbox is display-only is acceptable.
3. "Recently shipped" ordered by
  Source: review round 1
  Rejected. The walker bumps

  on every tracker upsert, not just on a status change, so it isn't a ship
        timestamp — using it would let a no-op re-walk reorder the group or resurface an old feature. Chosen instead: a dedicated done-transition event whose
        timestamp drives ordering and the recency cutoff.
4. Surface the tracker doc (

  ) as a card
  Source: review round 1 → decided with Nigel
  Rejected. The tracker is indexed with a null

  and would otherwise show up in "New since last visit" without a
        feature. Rather than give it a bespoke card shape, it (and any null-feature document) is filtered out of the unread feed.
5. Live, auto-refreshing inbox via HTMX/SSE
  Source: design doc topology (§6 —

  depends on

  )
  Deferred.

  is its own MVP feature that depends on this one. v1 is a plain server render with manual reload;
        pulling realtime in here would invert the dependency and inflate scope.
6. De-duplicate a feature/doc across categories
  Source: design doc (§6 design note — "show it in both with cross-references")
  Rejected. The categories answer different questions at different granularities; showing the same feature under "In progress" and a
        changed doc under "New since last visit" is intended, not noise. No cross-category de-dup, and the "cross-references" elaboration is out of scope here.

## Delivery phases

### Phase 1 — Inbox read model (+ the ship event)

The data layer the inbox derives from. Two parts: (a) extend the walker's tracker-diff path to emit a done-transition ("shipped") event when a feature's status flips to `done`, so "Recently shipped" has a real ship timestamp; (b) the three category derivations as connection-taking storage functions returning display-ready rows (project, feature, humanised label, last activity), with the cross-project and single-project variants, reusing the existing unread predicate and excluding non-active and null-feature docs from the unread feed. No UI. Delivers testable value: the ship event and the inbox's queries exist and are correct, exercised against a seeded temp DB (docs + events + read_state, status mixes, a done-transition) under the skeleton's no-network harness.

### Phase 2 — The Home page

The `/` route renders the three categories as cards through a Jinja template, replacing the placeholder index, cross-project by default and most-recent-first within each group. Cards show project + feature + humanised label + last-activity; a category with no rows is hidden, a fully empty inbox shows the "Nothing's waiting for you" state, and an unconfigured DB shows the "no data yet" state rather than erroring. Cards are display-only (no click target yet). Tested by seeding the DB and asserting the rendered inbox, including the empty and unconfigured states.

### Phase 3 — Per-project filter

Filter chips at the top of the page that narrow every category to a single project, "show all" being the default. Tested by asserting the filtered render contains only the chosen project's rows.

## Indicative implementation notes

Plan-level detail surfaced during requirements, to carry into `/feature-plan` — not binding.

- **Reuse the unread predicate.** `storage/read_state.py` already ships `unread_document_ids(conn, project_id=None)` (active docs with an event newer than `COALESCE(last_read_at, '')`, optional project filter). The "New since last visit" query should build on this rather than re-deriving the comparison; it needs enriching with join fields (project name, feature slug, doc type, max event timestamp) for the card, and an added `feature_id IS NOT NULL` (equivalently, drop `type='features'`) filter so the tracker and other null-feature docs don't surface.
- **The ship event.** The walker's `_apply_tracker_rows` path currently upserts feature rows unconditionally; add detection of an `old_status != 'done' && new_status == 'done'` transition and emit an `events` row for it (event type e.g. `shipped`). "Recently shipped" then selects features whose latest shipped event is within the window, ordered by that event's `created_at` desc, capped to a small N. Confirm the chosen event-type string and whether to backfill (existing done features have no shipped event yet — likely acceptable, they age out).
- **Category sources.** In progress / Recently shipped come from `features.status` joined to `projects`; in-progress ordering uses the feature's most-recent active-doc event.
- **Humanised labels.** A small mapping from `documents.type` to display strings ("requirements" → "Requirements", "plan" → "Plan", re-render/feedback variants folded sensibly). Plan-level detail; the requirement is just that cards show a readable label.
- **Replacing the placeholder.** Phase 2 changes the `/` route and the `index.html` template; the existing `routes_test.py` asserts the placeholder `MARKER`, so that test moves with the route.
- **DB-not-configured / empty handling.** The current `/` never gates on `db_path`; the admin routes return 503 when it's `None`. The inbox should branch to the "no data yet" render instead. Decide where the gate lives (route vs read-model returning empties).
- **Hot-path indexing.** The unread scan touches `events` per active document. `read-state` left open whether an index on `events(document_id, created_at)` is warranted or whether `doc-discovery`'s FK indexes suffice — revisit if the inbox query is slow.

## Design notes

Decisions and reasoning captured during review iteration.

- **"Recently shipped" uses a dedicated ship event, not `updated_at` (round 1).** The `updated_at` proxy isn't a ship timestamp — the walker bumps it on every tracker upsert — so a status-transition event is emitted and read instead.
- **Tracker / null-feature docs are filtered out of the unread feed (round 1).** Nigel: filter them out rather than give the tracker a bespoke card. Keeps the card model uniformly feature-shaped.
- **No cap on "New since last visit" (round 1).** Nigel: no cap — list every unread doc. Only "Recently shipped" is bounded.
- **No "data as of" freshness indicator in v1 (round 1).** Nigel: "I'm not really going to use this until it's all implemented, so no need to paper over little gaps like this." Principle — don't add scaffolding for a rough edge that disappears once the realtime/whole-flow features land; the inbox isn't a daily-driver until then.
- **Ordering most-recent-activity first, cross-project interleaved (round 1).** Every category sorts by recency; the cross-project view interleaves by recency rather than grouping per project.
- **Empty categories hidden; all-empty shows one state (round 1).** A zero-row category is omitted; "Nothing's waiting for you" appears only when all three are empty. DB-unconfigured shows a distinct "no data yet" state.
- **"Last activity" is computed over active docs only (round 1).** For feature-level cards, the most recent event across the feature's active documents, mirroring the unread feed's active-only scope.
