# feature-archive-semantics — Requirements

## Summary

The feature tracker already lets you retire a feature with the `drop` verb, but the retirement it records is *bare*: it sets the feature's status to `archived` and nothing more. There is no record of *why* the feature is gone, no pointer to *where the work went* if it went somewhere, and no way back — once dropped, a feature is stuck in `archived` forever.

That loses real information. Two concrete cases from the backlog show why the distinction matters:

- **Subsumed** — the outcome shipped, but folded into *another* feature rather than as its own line of work. Example: `semantic-carveout-llm-exemption-realiser` was delivered as Phase 3 of a different feature. It is not **Done** (it was never worked as its own thing) and it is not simply gone — it was absorbed, and the tracker should say so and point at the feature that absorbed it.
- **Superseded** — a design decision dissolved the need before the work was ever built. Example: `segment-carveout-routing-rationale` became moot when a redesign stopped doing segmentation routing carve-outs at all, so there was no rationale left to capture. This is not **Parked** (which might come back) — it is gone for a reason worth recording.

This feature **replaces** `drop` with a semantic `archive` verb: an archival carries a required **reason** (`subsumed` / `superseded` / `duplicate` / `obsolete`), an optional **superseded-by** pointer to where the work went, and an optional free-text **note**. Archiving becomes reversible via an `unarchive` path, and archived features render in a dedicated *Archived* section of the exported tracker — so “why it is gone and where it went” is legible at a glance without cluttering the Available list. It is the feature-layer sibling of `document-archive-api`, which does the same for individual documents; the two share the `reason` / `superseded_by` / `note` vocabulary.

## Vision

Archiving a feature records *why* it was retired and *where the work went*, is reversible if you got it wrong, and renders in a self-explaining Archived section — so a dropped feature leaves a legible trail instead of silently vanishing.

## Non goals

- **Document-level archival.** Retiring an individual superseded *document* is the sibling feature `document-archive-api`, not this one. This feature archives whole tracker rows (features).
- **Hard delete.** Archiving is non-destructive — the feature row and all its context / requirements / plan documents persist as an audit trail. There is no delete path; that is a deliberate constraint, not an omission.
- **Changing which lanes hide archived features.** The inbox and feature-choice already exclude `archived` features (`status NOT IN ('parked','done','archived')`); that behaviour is correct and unchanged. Only a new *rendered* Archived section is added.
- **A point-and-click archive UI in the webapp.** Archival is driven over the API (by the feature-choice / retro skills and by hand); this feature adds the endpoint and the read surfaces, not an in-page button.
- **Backfilling reasons onto existing archived features.** The features already dropped before this ships legitimately have no reason; they render with blank metadata rather than being retro-fitted.

## User stories

1. As a developer retiring a feature that shipped inside another, I want to archive it with a `subsumed` reason and a pointer to the feature that absorbed it so that when I archive `semantic-carveout-llm-exemption-realiser` pointing at the feature it was folded into, the tracker records that it was absorbed there — not that it was independently done, and not that it silently disappeared.
2. As a developer whose feature was made moot by a redesign, I want to archive it as `superseded` with a note explaining what changed so that archiving `segment-carveout-routing-rationale` leaves a one-line record that the routing carve-out was designed away, rather than leaving a future reader wondering whether it was forgotten.
3. As a developer who archived a feature too hastily, I want to unarchive it back to Available so that when a feature I dropped turns out to still be needed, I can restore it to the Available list with one call instead of editing the database by hand.
4. As a developer scanning the tracker, I want archived features shown in their own section with their reason and superseded-by pointer so that reading `features.md` I can see at a glance which retired features were absorbed elsewhere (and follow the pointer) versus which were designed away — without those rows cluttering Available.
5. As an agent driving the tracker over HTTP, a clear error when I archive with a reason that requires a superseded-by pointer but omit it so that an attempt to archive as `subsumed` with no pointer fails loudly at the write boundary rather than silently creating an orphan archive with no record of where the work went.

## Data model

Archival metadata lives on the existing `features` row — a feature has exactly one archival state, so no new table is warranted. Four new nullable attributes are added, populated when a feature is archived and cleared when it is unarchived:

An archival also writes a `feature_archived` event (the audit spine every tracker mutation already uses), carrying the reason and superseded-by so the history is queryable; `unarchive` writes its reverse. Because features dropped before this feature existed carry NULL metadata, every read surface must render a metadata-less archived row gracefully (blank cells, not a crash). `archived` is *already* a valid feature status; this feature adds the metadata, the reverse transition, and the rendering — not a new status.

## Technical approach

Follow the established tracker-verb pattern exactly: a `POST` to a verb subpath, an idempotent storage mutation returning a result with a `changed` flag, an event on real change, and an SSE broadcast. The new work is a semantic `archive` verb (plus its reverse) that carries metadata the bare `drop` never did.

- **One archival path — `drop` is retired.** The constraint is explicit: do not ship two verbs that both set `status='archived'` with divergent semantics. `drop` has no callers outside the webapp's own route table and OpenAPI spec (verified — no feature-* skill invokes it), so it is removed outright in favour of `archive`. Removing the advertised `/drop` route is a breaking change to the published `/openapi.json` contract; that is accepted (the route is uncalled at runtime).
- **Reason is required and gates superseded-by.** `archive` requires a `reason`. `subsumed`, `superseded`, and `duplicate` each name a retirement that went *somewhere*, so a superseded-by pointer is required for them; `obsolete` may stand alone. A missing-pointer-for-those-reasons request is rejected at the write boundary with a 4xx rather than storing an orphan archive.
- **Superseded-by is free text, resolved best-effort with a soft warning.** On archive, the pointer is stored verbatim (MR / decision refs are legitimate and must not be rejected). If it is slug-shaped and resolves to a real feature in the project, read surfaces link it; if it is slug-shaped but does *not* resolve, the response carries a non-blocking warning (the archive still succeeds) so a typo'd slug is surfaced rather than silently accepted.
- **Deliberate, so 404 on a missing feature.** Archiving a feature that does not exist returns 404, consistent with the other verbs — archival is an explicit act and a wrong slug should surface, not no-op.
- **Reversible.** An `unarchive` verb mirrors the existing transition guards: `archived` → `available` (clearing the archival metadata, emitting a `feature_unarchived` event); already-`available` is an idempotent no-op (`changed=false`); any other status is an `InvalidTransition` → 409. This is genuinely new — today nothing transitions out of `archived`.
- **Re-archive is a no-op.** Archiving an already-archived feature changes nothing (`changed=false`), *including* when the new request carries different metadata — correcting a wrong reason is done via `unarchive` then re-`archive`, not a silent in-place mutation.
- **Expose the fields on the read API.** The feature listing and single-feature endpoints must return the archival metadata so the exporter and the project page can render it — today they return only status/owner/notes. This makes the API phase a hard prerequisite for both rendering phases.
- **Render where retirement is read.** The exported tracker gains an Archived section (why + where-it-went columns); the webapp project page's existing archived grouping is enriched to show the same. Neither touches the inbox, which already hides archived features.

## Alternatives

1. Keep the `drop` verb name, or retain it as a metadata-less alias Context open question: “enrich drop, or add archive and retire drop?” — resolved in review round 1 Rejected in favour of a new `archive` verb with `drop` removed. `drop` reads as “discard” and does not convey the richer “retired for reason X, went to Y” semantics; a fresh verb name makes the tracker self-describing. Keeping `drop` as a deprecated alias was considered as insurance for the published API contract, but the breaking change was explicitly accepted (the route is uncalled at runtime), so a clean retire — and a required `reason` on every archive — won out over a null-reason alias.
2. Store superseded-by as a foreign key to `features(id)` Natural relational modelling of “points at another feature” Rejected. A superseder is not always a tracked feature — it may be an MR or a design-decision reference. A hard FK would reject those legitimate cases. Free text with best-effort resolution (link when it resolves, a soft warning when a slug-shaped value does not) keeps both cases working without rejecting valid input.
3. A separate `feature_archives` table Normalised modelling of archival as its own entity Rejected as over-built. A feature has exactly one archival state; four nullable columns on the existing row plus the event audit trail is the simpler shape, consistent with how owner/notes already live inline.
4. Share the reason enum + validation with `document-archive-api` via a common module Reviewer suggestion — resolved in review round 1 Deferred, not adopted. The document sibling is being implemented in parallel and it is unknown which lands first; coupling the two on a shared module now would create a merge dependency between two in-flight worktrees. The vocabulary is kept identical by convention for now; a shared module can be extracted once both have landed.

## Delivery phases

Phase 1 is a hard prerequisite for Phases 2 and 3 — neither rendering phase can show archival metadata until the read API exposes it.

### Phase 1 — The archive / unarchive API + migration (webapp)

Add the migration (the four nullable archival columns on `features`), the `archive_feature` storage mutation (required reason, the reason-gates-superseded-by rule, best-effort superseded-by resolution with a soft warning, no-op on re-archive, 404 on missing feature, event + broadcast on change), the reverse `unarchive` mutation (`archived` → `available`, clearing metadata), and their `POST` endpoints. Remove the `drop` verb (route, handler, mutation) and update its tests. Expose the archival fields on the feature listing and single-feature read endpoints. Cover the validation matrix — each reason with and without a pointer, the soft-warning path, idempotency, 404, the round-trip unarchive — with tests. On its own this delivers the full archival capability end-to-end over the API. One MR.

### Phase 2 — Project-page Archived rendering (webapp)

Enrich the project page's existing archived grouping to show each feature's reason, superseded-by (linked when it resolves to a sibling feature), and note, tolerating rows with NULL metadata. Touches the project-page handler and its Jinja template. Read-model and presentation only — no schema change. One MR.

### Phase 3 — Exported-tracker Archived section (feature-skills)

Teach the `feature-html-to-md` features renderer to emit a `## Archived` section (Feature / Reason / Superseded by / Note columns), sourced from the archival fields the Phase 1 API now returns, so `features.md` carries the retirement record. This lives in the `feature-skills` repo. One MR.

## Indicative notes

Plan-level detail worth carrying forward (not binding requirements):

- **Migration number is a live collision risk.** The next free number is 0009, but another feature (the sibling `document-archive-api`) is being implemented *in parallel in a separate worktree* and will also add a migration. Both cannot be 0009. This will not surface until the second of the two merges to `main`; the plan **must** include a merge-to-main checkpoint that re-checks the migration numbering against `main` and renumbers/reconciles whichever lands second (and re-runs the migration runner) rather than assuming 0009 is still free at merge time.
- **Column naming.** The `features` table already has a `notes` column; to avoid confusion the new columns are likely `archive_reason` / `superseded_by` / `archive_note` / `archived_at` at the DB layer, while the *API field names* stay `reason` / `superseded_by` / `note` to match the `document-archive-api` vocabulary. DB column vs API field may differ.
- **Migration style.** Mirror the existing `ALTER TABLE ... ADD COLUMN` migrations and the `INSERT INTO schema_version` footer, keeping comments semicolon-free (the runner naively splits on `;`). The runner is forward-only (no down-migrations); the change being additive nullable columns keeps this low-risk.
- **Events.** New archives emit `feature_archived` (carrying reason + superseded_by) and unarchives emit `feature_unarchived`; both record `actor` from the request body like the other verbs (the `events.actor` column exists, migration 0008). The old `feature_dropped` type has *no* runtime reader — changing the emitted type breaks only two test assertions — but historic `feature_dropped` rows will coexist with new `feature_archived` rows in the append-only log, so any future analytics over events must union both types.
- **Load-bearing accessors.** “Expose the fields” means editing two SQL statements: `list_features` (`storage/tracker.py:61`) and `get_feature` (`:73`), which return no archival fields today.
- **Phase 2 touch points.** The archived group renders via `web/project_page.py` (its `_feat()` dict, currently slug/owner/last_activity) *and* `web/templates/project.html` — the template is a required change site, not just the handler.
- **Phase 3 change site.** The live features renderer is `_render_features_md` in `feature-skills/bin/feature-html-to-md` (~L549), which hardcodes the four sections and drops archived at its `section is None → continue`. The `_STATUS_SECTIONS` / `_build_section_block` / `_format_row` merge-into-file machinery is the superseded path and is effectively dead — do not edit it. The real change is adding an Archived branch (and its four columns) to `_render_features_md`.
- **Test blast radius of retiring `drop`.** Removing the verb requires rewriting/removing its tests across `storage/tracker_test.py`, `web/tracker_test.py`, `web/project_page_test.py` (which uses `/drop` to set up archived state — switch it to `/archive`), and `web/openapi_test.py` (route-enumeration set). Mechanical, but real.
- **Unarchive target status.** Recommended `archived` → `available` (owner already null after archive). Restoring the pre-archive status (e.g. `in_progress` with the prior owner) would need the event history and is not proposed.

## Design notes

Decisions taken in review round 1 (source: requirements-feedback #1):

- **Retire `drop` outright; `reason` is required** on `archive` — the breaking change to the published OpenAPI contract was explicitly accepted (`drop` is uncalled at runtime). Chosen over keeping a metadata-less `drop` alias.
- **Superseded-by is free text with best-effort resolution + a soft warning** when a slug-shaped value doesn't resolve — not a hard FK (MR/decision refs stay valid) and not silent (typos are surfaced).
- **`unarchive` mirrors the existing verb matrix**: archived → available (clears metadata, emits `feature_unarchived`), available = no-op, else 409.
- **Re-archive is a no-op regardless of metadata** — correction is unarchive + re-archive, not silent in-place mutation.
- **Existing archived rows render with blank metadata**; no backfill.
- **`subsumed` / `superseded` / `duplicate` require a pointer; `obsolete` may stand alone** (confirmed — a duplicate names what it duplicates).
- **Enum/validation kept independent of `document-archive-api` for now** — the two are in-flight in parallel worktrees and it is unknown which lands first, so no shared-module merge dependency is created; vocabulary stays identical by convention.
