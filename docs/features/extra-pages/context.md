# extra-pages

## Problem space and motivation

The inbox shipped as the headline UX and the doc-view closed the click-through loop, but the webapp is still inbox-and-doc only. Three rough edges surfaced while using it day-to-day:

### 1. No way to clear "New since last visit" in one action

The "New since last visit" category accumulates and there's no bulk "mark all read" affordance on it. `read-state` already shipped `mark_all_read` and exposes it as `POST /admin/projects/{project}/mark-read`, but that's per-project and has no UI surface in the inbox. The headline list a user actually looks at — the cross-project "New since last visit" feed, including the "All" filter — has no button to dismiss the lot.

### 2. No feature page

From the inbox you can open an individual doc, but there's no page that shows *all* docs for a given feature (its context, requirements, plan, feedback, etc. in one place). The feature name on each inbox card is plain text; it should link somewhere sensible — a per-feature page. `doc-view` already builds breadcrumbs from a `documents → projects LEFT JOIN features` lookup, so the data to group docs by feature is already indexed.

### 3. No project page

Likewise there's no page listing the features within a project. The project name on each card (and the existing project filter chips) could link to a project page showing that project's features, each linking on to its feature page. This gives the natural inbox → project → feature → doc drill-down.

### 4. Inbox badges are visually undifferentiated

The doc-type badges on inbox cards (Plan / Requirements / Context / Feedback) are all rendered with the same accent styling (`.card-label`), so they convey different meanings but look identical at a glance. They should be visually distinguishable by type (e.g. per-type colour or treatment) so the inbox is scannable.

## Related work

- **`read-state`** (shipped) — already has `mark_all_read` and the `POST /admin/projects/{project}/mark-read` endpoint; the bulk-clear affordance is partly a UI gap, partly a question of cross-project scope (see open questions).
- **`inbox-view`** (shipped) — the read-model layer in `storage/inbox.py` (`build_inbox` and the per-category builders) and the project filter chips in `web/templates/_inbox_body.html`. Cards already carry `project`, `feature`, `label`, `document_id`.
- **`doc-view`** (shipped) — `web/doc_view.py` routes docs by `document_id` and builds breadcrumbs via the `documents → projects → features` join; sibling prev/next nav already orders a feature's active docs by `DOC_TYPE_ORDER`. A feature page is essentially that grouping promoted to its own route.
- The SQLite schema (§4) already has `projects`, `features` (with `status`/`owner`/ `notes` from tracker parsing), and `documents` with `project_id`/`feature_id` — the joins for both new pages exist.

## Constraints and considerations

- Server-rendered Jinja, no HTMX — match the existing inbox/doc-view pattern. SSE-driven live refresh (`sse-refresh`) is in play for the inbox; consider whether the new pages need the same live behaviour or can be static-on-load.
- Routing convention so far is `document_id`-keyed for docs. Feature and project pages need their own URL scheme — likely keyed by project name + feature slug (human-readable) rather than surrogate ids, but that's a requirements decision.
- Bulk mark-read scope: the inbox "New since last visit" feed is cross-project, but the existing endpoint is per-project. Clearing the "All" view either needs a project-less variant or a fan-out over the visible projects.
- Read-state interaction: a feature/project page that renders docs should be deliberate about whether merely listing a doc stamps it read (it shouldn't — only opening the doc does, per the doc-view per-render stamp).
- Badge differentiation should stay within the existing dark palette and degrade gracefully for unknown/new doc types.

## Open questions

1. Should "mark all read" on "New since last visit" respect the active project filter (clear only the filtered project) or always clear everything currently shown? And does the existing per-project endpoint get a cross-project sibling, or does the UI fan out?
2. How are feature and project pages addressed — by readable `project/feature` path, or by id? What happens for the null-feature tracker doc and archived/missing docs on these pages?
3. What does a feature page show beyond its docs — status, owner, notes from the tracker? Does it surface the synthesis-response / comments state per doc?
4. Should the project page double as (or replace) the project filter chip target, or is it a separate destination?
5. Is per-type badge colour the right treatment, or should type be conveyed another way (icon, grouping, ordering)? Is there an agreed type→colour mapping?
6. Do the new pages need SSE live-refresh, or is reload-on-navigate acceptable?
