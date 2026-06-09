# extra-pages

## Problem

The webapp ships an inbox and a doc-view, but it's only those two surfaces. Four day-to-day rough edges have surfaced:

### 1. "New since last visit" can't be cleared in one action

The headline list grows and the only way to clear it is opening every doc. `read-state` already has `mark_all_read` behind `POST /admin/projects/{project}/mark-read`, but it's per-project and has no button in the inbox. The list the user actually looks at — including the cross-project "All" view — has no dismiss-the-lot affordance.

### 2. There's no per-feature page

From the inbox you can open one doc at a time, but nothing shows all of a feature's docs together (context, requirements, plan, feedback). The feature name on each inbox card is plain text and links nowhere. The data is already indexed — `doc_view.py` builds breadcrumbs and sibling-nav from exactly this grouping.

### 3. There's no per-project page

Nothing lists the features within a project. The project name on a card and the filter chips are the only project-level surfaces, and neither leads to an overview of that project's features and their status.

### 4. Inbox badges are visually undifferentiated

The doc-type badges (Context / Requirements / Plan / Review / Feedback / Shipped) all render with the same accent styling (`.card-label` — amber on dark amber). They convey different meanings but look identical, so the inbox doesn't scan by type at a glance.

## Vision

The inbox becomes navigable: badges read at a glance, "New since last visit" clears in one click, and project and feature names are doorways into overview pages that drill down inbox → project → feature → doc.

## User stories

1. As a developer triaging the inbox
  I want to mark everything in "New since last visit" as read in one action
  I've skimmed the list, nothing needs opening right now, and I want a clean inbox next visit without clicking into a dozen docs.
2. As a developer triaging a single project
  I want "mark all read" to respect the active project filter
  I've filtered to

  , dealt with its new docs, and want to clear just kea's unread without touching

  .
3. As a developer returning to a feature
  I want one page showing all of a feature's docs and its status
  I'm picking

  back up and want its context, requirements, plan and any feedback in one place rather than hunting card-by-card in the inbox.
4. As a developer following a card
  the feature and project names on inbox cards and in doc breadcrumbs to be links
  I'm reading a plan doc and want to jump up to the whole feature, or across to the project's other features, without going back to the inbox and scrolling.
5. As a developer surveying a project
  I want a page listing a project's features and their status
  I want to see what's in progress, available and shipped in

  at a glance, and click through to any feature.
6. As a developer scanning the inbox
  doc-type badges to be visually distinct by type
  A screen of mostly Context cards with one Requirements among them should let me spot the Requirements instantly by colour, not by reading each label.

## Data model

No new tables and no migration. Everything here is read-model and presentation over the existing schema:

- **Feature page** reads a feature's row from `features` (status, owner, notes) joined to its `documents` (active and archived). Primary docs (`context`/`requirements`/`plan`/`review`) are ordered the way `doc-view` already orders siblings; feedback/synthesis docs are read separately so they don't fall into an undifferentiated trailing bucket.
- **Project page** reads `projects` joined to its `features` across all three statuses (`in_progress` / `available` / `done`), with each feature's last-activity derived from `events` the same way the inbox's "in progress" builder already does.
- **Mark-all-read** reuses the existing `read_state` writes. The set stamped is exactly the documents that "New since last visit" currently shows (which already excludes unanswered feedback / "Awaiting your input"), scoped to the active project filter — not "every active doc in the project".
- **Badges** need the raw doc-type slug carried on the inbox card alongside the humanised label (today only the label is exposed); no stored data changes.

## Technical approach

### Mark all read

Add a "Mark all read" control on the "New since last visit" section header. It clears *exactly that list* — the documents currently shown under "New since last visit", scoped to the active project filter (just the filtered project when a chip is active, all projects on the "All" view). It deliberately does *not* stamp "Awaiting your input" docs (unanswered feedback you shouldn't silently dismiss) or every active doc in the project. A single server call computes and stamps that set; we don't fan out N requests from the browser.

The action returns how many docs it stamped, surfaced as a lightweight "marked N as read" confirmation (no undo, no confirm dialog — read-state is low-stakes and a prompt on a triage action is friction). After the post, the inbox refetches in place so the list empties. Marking read is a read-state change, not a doc change, so it does not go through the walker/broadcaster; the button refreshes its own view.

### Feature and project pages

Two new server-rendered Jinja pages, matching the existing inbox/doc-view pattern (no HTMX). Unlike docs — which are routed by surrogate `document_id` because source paths aren't safe to expose — these pages are navigational landmarks, so they're keyed by readable identifiers: a project by its name, a feature by its project name plus slug. Names and slugs are URL-encoded in links and matched decoded so awkward characters don't break routing. Unknown project/feature names **404** (you navigated to a specific thing that should exist), rather than the inbox filter's degrade-to-empty behaviour.

The **feature page** shows the feature's status, owner and tracker notes, then its primary docs ordered by the existing `DOC_TYPE_ORDER`, each linking to `/doc/{id}`. Active feedback/synthesis docs are grouped in their own subsection (not interleaved), and an unanswered one is badged "Awaiting your input" reusing the inbox's existing predicate. Archived docs sit in a de-emphasised "Archived" subsection ordered by type then recency (doc-view already renders archived docs, so links stay valid; links to missing-on-disk docs also stay valid — the raw route already degrades gracefully). Its breadcrumb is `Project ▸ Feature`.

The **project page** lists the project's features grouped by status (In progress / Available / Done), mirroring the features tracker's own sections, each feature linking to its feature page. A feature with no docs still appears with its status and a "no docs yet" note; a project with only available features renders the list, not an empty state. The project-level tracker doc (`feature_id = NULL`) surfaces here as the project's tracker, not on any feature page.

These pages become link targets from existing surfaces. On inbox cards — across *all* categories, including "Recently shipped" — the feature name links to its feature page (built from project + slug, since slugs are unique only within a project) and the project name to its project page. The null-feature tracker card links only its project name. And the doc-view breadcrumbs, today plain text with no hrefs (their tuples already carry a nullable href slot), gain links to the feature and project pages — so reading a doc you can jump up to the feature or across to the project. Listing a doc on these pages must not stamp it read; only opening `/doc/{id}` does, preserving the current read-state semantics.

### Badges

Carry the raw doc-type slug on each inbox card and key a per-type CSS class off it, giving each type a distinct colour within the existing dark palette. The text label stays in the badge, so differentiation is colour *and* label, never colour alone — which keeps it legible for colour-blind users. Committed mapping: Context amber (the current default), Requirements blue, Plan violet, Review teal, Feedback magenta, Shipped green (the existing done-accent), In progress neutral grey. Unknown or new types fall back to the neutral accent styling so nothing breaks when a new doc type appears.

### Live refresh

The feature and project pages are wired into the same SSE channel as the inbox (`/events`): when a walk commits a change they refresh in place, reusing the inbox's `EventSource` pattern, so a doc landing while you sit on a feature page shows up without a manual reload.

## Alternatives considered

1. Reusing

  as-is for the button
  Source: round 1 review
  The shipped

  stamps every active doc in a project — broader than the "New since last visit" list the button sits on, and it would silently dismiss "Awaiting your input". Rejected in favour of stamping exactly the shown set (the inbox's existing unread predicate already produces it).
2. Browser fan-out for cross-project mark-read
  Source: considered against the existing per-project endpoint
  The "All" view could POST once per project from JS. Rejected: a single server call is simpler, atomic, and avoids N round-trips and partial-failure states.
3. Deferring SSE on the new pages (reload-on-navigate v1)
  Source: round 1 review — decided with user
  Initially proposed as a v1 cut. Rejected: the feature page is exactly where a developer sits and reads while a new doc might land, so staleness bites most there. The SSE channel already exists; wire the pages into it.
4. Routing pages by surrogate id
  Source: doc-view's document_id convention
  Docs are id-routed because source paths are unsafe to expose. Features and projects have no such hazard and benefit from readable, shareable URLs, so they're keyed by name/slug instead.
5. Per-type icons instead of colour
  Source: badge-differentiation options in the context doc
  Icons add an asset/representation question and read less quickly than colour for a small fixed type set. Colour (plus the existing label) is the lighter, more scannable treatment; revisit if the type set grows.

## Delivery phases

Ordered cheapest-and-most-independent first. Each phase is one MR delivering testable value on its own.

### Phase 1 — Differentiated doc-type badges

Inbox badges become visually distinct per type, with a graceful fallback for unknown types. Self-contained: read-model carries the type slug, template + CSS do the rest. Immediate visual win, no new routes.

### Phase 2 — "Mark all read" on New since last visit

A control on the section header that clears exactly the "New since last visit" set, respecting the active project filter (and clearing across all projects on "All"), with a "marked N as read" confirmation. The list empties in place via the existing fragment refetch.

### Phase 3 — Feature page

A per-feature page (status, owner, notes; primary docs ordered by type, a grouped feedback subsection, and a de-emphasised archived subsection — each doc linking through). Reachable from inbox cards' feature names and the doc-view feature breadcrumb (the doc-view crumbs become links in this phase). Live-refreshes on the SSE channel.

### Phase 4 — Project page

A per-project page listing features grouped by status (In progress / Available / Done), each linking to its feature page, plus the project's tracker doc; reachable from inbox cards' project names and the doc-view project breadcrumb. Live-refreshes on the SSE channel. Completes the inbox → project → feature → doc drill-down.

## Indicative implementation notes

Plan-level pointers, not binding:

- **Mark-read:** the set to stamp is exactly what `new_since_last_visit` (storage/inbox.py) returns for the active filter — reuse it (or the closely-related `unread_document_ids`, minus the awaiting-input feedback exclusion the inbox query already applies) rather than `mark_all_read`. Likely a new bulk endpoint (project-less or id-taking) alongside the existing `POST /admin/projects/{project}/mark-read` (routes.py), returning the stamped count for the confirmation. After the POST, reuse the inbox's existing in-place refetch.
- **Badges:** `InboxCard` (storage/inbox.py) currently exposes `label` only; add the raw `doc_type` slug. `_inbox_body.html` keys a class off it; CSS extends `.card-label` per the committed colour mapping. "Shipped" and "In progress" are synthetic labels, not document types — they get their own classes (green / grey) keyed off the card rather than a doc-type slug.
- **Pages:** new route handlers + templates registered in `web/app.py`; reuse the inbox read-model query shapes (the `documents → projects → features` joins and `DOC_TYPE_ORDER` already in inbox.py / doc_view.py). Breadcrumb tuples in `doc_view.breadcrumbs` already carry a nullable href slot — populate it for the project and feature crumbs.
- **SSE:** the pages can reuse the inbox's `EventSource` + debounced-refetch pattern (`index.html` / `/events`); a per-page fragment endpoint or a full refresh on the contentless `changed` message both work.
- **URL shape** (for the plan to settle): e.g. `/project/{name}` and `/project/{name}/feature/{slug}`, URL-encoded.

## Design notes

- **Round 1:** "Mark all read" stamps exactly the "New since last visit" set (not all active docs), scoped to the active project filter, leaving "Awaiting your input" untouched — and shows a "marked N" confirmation.
- **Round 1:** The new pages are wired into SSE rather than reload-on-navigate — staleness matters most on the feature page, and the channel already exists.
- **Round 1:** Doc-view breadcrumbs become clickable as part of this work (not just the new pages' own crumbs).
- **Round 1:** Badges differentiate by colour *and* label (never colour alone), with a committed type→colour mapping, for colour-blind legibility.
- **Round 1:** Unknown project/feature names 404; feedback docs grouped (not interleaved) on the feature page; the null-feature tracker doc surfaces on the project page only.
- **Out of scope (round 1):** unread-count indicators on project/feature links — a natural adjacent win, deferred.
