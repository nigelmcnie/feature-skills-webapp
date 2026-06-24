# tracker-drop-verb — Requirements

## Summary

The tracker lets you **capture**, **claim**, and **ship** a feature, but it has no way to *remove* one. So a feature you decide not to build sits in the *Available* bucket forever — and worse, every time we regenerate `features.md` from the database it pops straight back, even if someone deleted the row from the file by hand.

This feature adds a **drop** action. Dropping a feature moves it to a new terminal status, **archived**: the row, its documents, and its event history all stay in the database, but it disappears from the *Available* list, from every inbox lane, and from the `features.md` export. Dropped features remain discoverable in a collapsed *Archived* section on the project page.

For example: you captured `rule-ir` a few weeks ago, then decided it's superseded and won't be built. You call drop on it. It leaves the Available list and stops re-surfacing in `features.md` and the inbox — but it's still listed under Archived on the project page, and its context doc and event trail remain at its `/doc` URL if you ever want to recall why it was dropped.

## Problem

The tracker's lifecycle is strictly one-directional: `capture` → `available`, `claim` → `in_progress`, `ship` → `done`. There is no transition that says *“this feature won't be done.”* A feature abandoned in spirit has nowhere to go, so it stays `available` in the database.

This was hit concretely during the skills-api-cutover review (the `synthesis-count-integrity-guard` work, 2026-06-21): a feature that should have gone away kept lingering. The only available workaround is a direct hand-edit of the SQLite `features` row — exactly the manual database surgery the agent-submission tracker work set out to eliminate.

The leak is made permanent by the export path. The `feature-html-to-md --merge-features` tool rebuilds the *Available* section of `features.md` from the live database on every run. So even if a human deletes the row from `features.md`, the next export re-creates it from the still-`available` DB row. There is no durable way to make a feature stay gone.

## Scope

**In scope:**

- A `drop` transition (HTTP `POST`) that moves a feature to a new terminal `archived` status.
- Removing archived features from every surface they could re-appear in: the project page's active buckets, the `features.md` merge-export (already excluded — see Technical approach), and the two *document-driven* inbox lanes (“New since last visit” and “Awaiting input”), which need an explicit feature-status filter because they key off document status, not feature status.
- A read-only, collapsed *Archived* section on the project page that lists dropped features, so a dropped feature stays discoverable without having to know its `/doc` URL.

**Out of scope** (see Non-goals for why):

- Un-dropping / restoring an archived feature (the Archived section is display-only).
- The non-terminal `parked` status and the `release`/`unclaim` reversal of `claim` — the adjacent `tracker-lifecycle-transitions` feature.
- Any CLI subcommand or feature-skill UI for dropping.
- Clearing the features *already* stuck as `available`: this feature delivers the mechanism; archiving the existing strays is a one-off operational follow-up (a handful of `drop` calls after deploy), not part of the MR.

## Vision

A feature you decide not to pursue can be dropped once and stays out of Available, the inbox, and every export — kept discoverable in a collapsed Archived section with its history intact — without touching the database by hand.

## Non goals

- **Un-dropping / restoring an archived feature.** The Archived section is read-only. The row is preserved, so a restore remains cheap to add later — most naturally as part of `tracker-lifecycle-transitions`, which already owns resume/release semantics.
- **Non-terminal parking or release/unclaim.** A separate feature (`tracker-lifecycle-transitions`). Decided with the user (2026-06-24) to keep this MR scoped to the terminal-drop leak, even though the two touch the same code and read-sites.
- **A hard delete that destroys the row and its documents.** Explicitly rejected in favour of the reversible, history-preserving archived status — see Alternatives.
- **Deactivating the feature's documents on drop.** Documents stay `active` so they remain readable at their `/doc` URLs; the inbox leak is closed by filtering on feature status instead — see Alternatives.
- **CLI or skill affordances.** The tracker verbs (`capture`/`claim`/`ship`) are API-only today; `drop` is API-only too.

## User stories

1. As a developer triaging the tracker
  I want to drop a feature I've decided not to build, so it
        leaves Available and stops re-appearing in

  and the
        inbox
  I captured

  weeks ago; it's now
        superseded. I POST

  on it. It disappears from the Available
        list and the inbox immediately and is absent from the next

  export.
2. As a developer revisiting past decisions
  I want to find a dropped feature again — and read why it was
        dropped — without knowing its

  URL
  Weeks after dropping

  I open the
        project page, expand the collapsed

  section, and click
        through to its context doc and event trail, which are still in the
        database.
3. As an automated agent (the feature skills)
  I want

  to behave like the other
        transitions — idempotent, 409 on an illegal move — so I can call it without
        special-casing
  A retry calls

  twice. The second call
        is a no-op that returns the archived state rather than an error, so the
        retry is safe.

## Data model

A new terminal value, `archived`, joins the feature status set: `available`, `in_progress`, `done`, `archived`. Status continues to be enforced in application code, not a database `CHECK` constraint — consistent with the existing model, where `features.status` is free text and the legal values live only in the `FEATURE_STATUSES` constant and the transition functions. No column migration is required.

Dropping records a history event row (mirroring `feature_claimed` and `shipped`), so the drop is auditable. A dropped feature keeps its `owner` as a historical record — unlike the `release` reversal in the adjacent feature, which clears it; drop is terminal, so the owner of abandoned in-progress work stays attached for the record. The feature's *documents* remain `active` (so they stay readable at their `/doc` URLs); they are kept out of the inbox by filtering on feature status, not by deactivating them.

## Technical approach

Add `archived` to the allowed status set and a `drop` transition that mirrors the existing `claim`/`ship` mutation contract: redundant transition is an idempotent no-op (no event, no broadcast); a not-found feature is a 404; an illegal source status is a 409; a real state change emits a history event and an SSE broadcast.

The legal moves into `archived` are `available → archived` (the core lingering-in-Available case) and `in_progress → archived` (abandoning claimed work). `done → archived` is rejected — a shipped feature is already terminal and already excluded from Available, so archiving it serves no purpose within this scope.

**Read-sites.** Two kinds exist, and they behave differently:

- *Feature-status views* — the project page's Available/In-progress/Done buckets and the inbox's “In progress” lane — match directly on feature status, so an `archived` feature falls out automatically; no change needed. We additionally **add a fourth, read-only *Archived* bucket** and render it as a collapsed section on the project page.
- *Document-driven views* — the inbox's “New since last visit” and “Awaiting input” lanes — select *documents* by document status and join to the feature only for display, so they do **not** exclude archived features for free. Each needs an explicit `AND f.status != 'archived'` (the feature JOIN already exists). This keeps the documents themselves active and readable at their `/doc` URLs.

**features.md merge-export.** No companion change is needed in the separate `feature-skills` repo — but not because the API filters the data. `list_features` still returns the archived row; the safety lives in the merge tool's section-placement logic, which is a status *allow-list* (`_STATUS_SECTIONS` / `_SECTION_TO_STATUS` in `feature-html-to-md`): it only ever places rows whose status is one of available/in_progress/done, with no fallback re-append, so an `archived` row lands in no section and is dropped. Verified by tracing all three placement passes (2026-06-24). **Caveat:** this is a cross-repo behavioural coupling that nothing in either repo's test suite pins — a future change to that allow-list would silently re-open the leak.

## Testing

Cover the transition matrix and the edges, asserting observable state (the stored status, the presence/absence of an event row, the bucketed read-site output) rather than internal calls:

- `available → archived`: status becomes archived, a drop event is recorded, the result reports a real change, the SSE broadcast fires.
- `in_progress → archived`: allowed; the owner is retained.
- `done → archived`: **rejected with a 409**. This is a rejected transition from a *different source status* — not the same shape as the `done → done` idempotent no-op; assert the error, not a silent no-op.
- `archived → archived`: idempotent no-op — no change, no event, no broadcast.
- Drop on a non-existent feature: 404.
- Handler-level malformed input (invalid JSON, non-object body): 400, matching the claim/ship handlers.
- Project page: a dropped feature appears in the new *Archived* section and in none of the three active buckets; `list_features` still returns it.
- Inbox exclusion: with a dropped feature that has an `active` document which would otherwise surface (an unread event for “New since”; a feedback doc with no synthesis for “Awaiting input”), assert it is *absent* from both lanes — this pins the `f.status != 'archived'` filter, which is the regression most likely to be reintroduced.

The merge-export's drop-on-unknown-status behaviour lives in the `feature-skills` repo and is out of this feature's test scope; it is verified here by inspection. A regression guard for it would belong in that repo — noted, not built here.

## Alternatives

1. Hard

  endpoint
  From the tracker description's options; chosen against
        with the user, 2026-06-24
  Deleting the row (cascading its documents) is simpler
        state but loses all history and is irreversible. The archived status
        preserves the row, docs, and event trail — letting you recall why a feature
        was dropped and restore it later — and mirrors the existing additive status
        model rather than introducing destructive deletion.
2. Deactivating the feature's documents on drop
  Considered in feedback round 1, 2026-06-24
  Setting the feature's documents to a non-active status
        would remove them from the document-driven inbox lanes too. But it's
        broader — it touches every

  read-site and
        would require

  to keep serving non-active docs at their

  URLs. Chose the targeted query filter
        (

  on the two affected lanes), keeping the
        documents active and readable.
3. Merge with

  into one “lifecycle completeness” feature
  Raised as an open question in that feature's context;
        decided with the user, 2026-06-24
  Combining terminal drop with the non-terminal

  status and

  reversal would be one
        coherent pass over the status model, but a larger feature. Kept separate to
        keep this MR focused on the concrete leak (features re-surfacing in
        Available); the lifecycle work lands afterward on the same code.

## Delivery phases

### Phase 1 — Drop transition and exclusion

The core fix: add `archived` to the status set, add the `drop_feature` storage transition and its `drop_handler` + route, and add the `f.status != 'archived'` filter to the two document-driven inbox lanes. With this phase a dropped feature stops surfacing anywhere — Available, inbox, and the `features.md` export. Includes the transition-matrix and inbox-exclusion tests. One reviewable MR.

### Phase 2 — Archived section on the project page

Add the read-only `archived` bucket in `project_page.py` and render it as a collapsed *Archived* section in `project.html`, with a test that a dropped feature shows there and nowhere else. Delivers discoverability — a dropped feature can be found again without its `/doc` URL. Separable from Phase 1 and independently shippable.

## Indicative notes

Plan-level detail to carry forward (not requirements constraints):

- **Storage** (`storage/tracker.py`): add `"archived"` to `FEATURE_STATUSES`; add `drop_feature(conn, *, project, slug, now) -> MutationResult` shaped like `claim_feature`/`ship_feature`. Insert a `feature_dropped` event row (matching the `feature_claimed`/`shipped` naming, `document_id NULL`).
- **Legal transition matrix**: `available→archived` ok; `in_progress→archived` ok; `archived→archived` idempotent no-op; everything else (incl. `done`) raises `InvalidTransition` → 409.
- **HTTP** (`web/tracker.py` + `web/app.py`): add `drop_handler` following the claim/ship handler shape (JSON body validation, `transaction`, map `FeatureNotFound` → 404 and `InvalidTransition` → 409, broadcast only when changed). Route: `POST /api/projects/{project}/features/{feature}/drop`. No request body fields are required (unlike claim's `owner` / ship's `outcome`).
- **Inbox** (`storage/inbox.py`): add `AND f.status != 'archived'` to the `new_since_last_visit` (line ~165) and `awaiting_input` (line ~257) queries. The `in_progress` lane already filters on feature status and needs nothing.
- **Project page** (`web/project_page.py` + `templates/project.html`): add `archived = [f for f in feats if f["status"] == "archived"]`, pass it to the template, and render a collapsed `<details>` Archived section (read-only; links to each feature's docs).
- **No DB migration** needed for the column.

## Design notes

- **Archived status over hard delete** (user, 2026-06-24): preserves the row, documents, and event history; reversible later; mirrors the additive status model.
- **Drop-only scope** (user, 2026-06-24): `tracker-lifecycle-transitions` (parked + release/unclaim) stays a separate feature, landing after this one on the same code.
- **Inbox guarantee rests on a feature-status filter, not allow-lists.** The document-driven inbox lanes key off document status, so excluding archived features needs an explicit `f.status` clause; documents stay active and readable at their `/doc` URLs.
- **Merge-export guarantee rests on the merge tool's placement allow-list** (`_STATUS_SECTIONS`/`_SECTION_TO_STATUS` in `feature-html-to-md`), not on API filtering — `list_features` still returns archived rows. This is a cross-repo coupling unpinned by tests in either repo.
- **Naming**: verb `drop`, resulting status `archived` — follows the existing verb≠status pattern (`claim`→`in_progress`, `ship`→`done`).
- **Owner retained** on `in_progress→archived` (terminal historical record), unlike the `release` reversal which clears it.
- **`done→archived` rejected** as out of scope: done is already terminal and excluded from Available. Revisit only if a “hide old shipped work” need appears.

## Review decisions

**Round 1 (2026-06-24).**

- **Inbox doc-driven lanes:** close the leak with a feature-status filter on the two lanes (chosen over deactivating the feature's documents).
- **Recoverability:** add a read-only collapsed *Archived* section to the project page now (rather than deferring any Archived UI), so dropped features stay discoverable. Split into delivery Phase 2.
- **Merge-export reasoning:** corrected to name where the guarantee actually lives (the merge tool's placement allow-list), and flagged the unpinned cross-repo coupling.
- **Existing strays:** clearing features already stuck as available is an operational follow-up, not part of the MR.
