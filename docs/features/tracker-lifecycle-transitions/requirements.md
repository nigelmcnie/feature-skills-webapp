# tracker-lifecycle-transitions — Requirements

## Summary

The tracker records where every feature sits: **Available**, **In Progress**, or **Done**. Today it only knows how to move *forwards* — capture a feature (it lands in Available), claim it (Available → In Progress), ship it (In Progress → Done). Three things we actually do in practice have no button:

- **Set a feature aside without abandoning it.** You start something, then decide it's not for now — but it isn't dead either. There's no "parked" state, so today we paste a `PARKED` banner into the top of the doc and the tracker still shows the feature as In Progress. The banner is invisible to every list and inbox.
- **Hand a claimed feature back.** You claim something, then realise you won't do it now. The only way back to Available is hand-editing the SQLite row — exactly the manual surgery the tracker was built to remove.
- **Mark an already-shipped feature done.** A feature got built and merged but was never tracked through In Progress, so it still sits in Available looking like nobody's touched it. There's no way to mark it Done without first pretending to claim it.

This feature adds those three moves: a non-terminal **parked** status (with a `park` transition and resume-by-claim), a **release** transition (In Progress → Available, the inverse of claim), and a **backfill** path (Available → Done) for shipped-but-mistracked features. All three follow the existing mutation contract. Crucially, **parked work stays visible** — it gets its own bucket on the project page, a category in the inbox, and a section in the exported `features.md` — so deferring a feature never makes it disappear.

## Problem

The lifecycle is one-directional and incomplete. `FEATURE_STATUSES` is `(available, in_progress, done)`, and the only transitions are `claim` (available → in_progress) and `ship` (in_progress → done). Three moves that came up in real use have no path:

- **Park a feature.** Setting a feature deliberately aside — finalised or partway, but not being worked on and not abandoned — has no representation. Hit while working a kea feature (`review-severity-recalibration`): the user noted "we don't have a way in the system to mark something as parked", so we fell back to jamming a `PARKED` banner into the top of the context and requirements doc bodies. The banner is invisible to the tracker, so the feature still reads as In Progress in every view — inbox, project page, and the `features.md` export.
- **Release / unclaim.** Demoting an In Progress feature back to Available — the inverse of `claim` — has no API or CLI path. To move `review-severity-recalibration` back to Available after parking it, the only option was a direct hand-edit of the SQLite `features` row (`status='available', owner=NULL`) via the app's connection helpers, plus a manually-inserted `feature_released` event for history. That hand-editing is exactly what the agent-submission tracker work set out to eliminate.
- **Backfill a shipped feature.** A feature that was built and merged but never tracked through In Progress is stuck in Available — `ship` only accepts `in_progress`, so the only way to mark it Done is to claim it first (recording a fiction) and then ship it. There's no honest one-step way to say "this was already shipped".

The throughline: the tracker can record progress but not *correction* or *deferral*. Every gap above has been worked around by hand-editing state the system is supposed to own.

## Scope

**In scope** — completing the non-terminal lifecycle, substrate-first (API + storage), with every read site kept honest and parked work kept visible:

- A `parked` status (deferred-but-alive) and a `park` transition from Available and In Progress; parking **clears the owner** (recording the prior owner in the event). Resuming a parked feature is a normal `claim` (parked → in_progress). Shipping a parked feature directly is rejected (409) — resume it first.
- A `release` transition: In Progress → Available, clearing the owner — the inverse of `claim`.
- A backfill path: Available → Done, for features shipped without ever being claimed.
- **Surfacing parked work** in every read site: a Parked bucket on the project page, a Parked category in the inbox (so deferred work isn't silently forgotten), and confirming the single-feature page renders a parked feature. Parked is excluded from the Available / In Progress / Recently-shipped buckets but is never simply dropped.
- **Cross-repo:** a `## Parked` section in the `feature-html-to-md` exporter (feature-skills repo) so the committed `features.md` reflects parked features rather than omitting them.
- A one-off dog-food correction: once the above lands, park `review-severity-recalibration` properly via the new endpoint and remove its doc-body banner.

**Out of scope** (see Non-goals for why):

- Terminal removal / archive — owned by the adjacent `tracker-drop-verb` feature.
- Reworking status enforcement into a DB `CHECK` constraint.
- A dedicated CLI / `/feature` park action. Interim invocation is a manual `curl`/POST — the same mechanism the feature-* skills already use for `claim`/`ship`. Whether a first-class client is warranted is an open question to revisit later (see Design notes).

## Vision

Every real lifecycle move — defer, hand back, and record-as-shipped — is a first-class tracker transition with history and a live-updating, visible view, so feature state is never again corrected by hand-editing the database and deferred work is never silently lost.

## Non goals

- **Terminal drop / archive.** `tracker-drop-verb` (Available) owns removing a feature so it stops re-surfacing as available — that's *terminal*. This feature is strictly *non-terminal*: a parked feature stays alive and claimable. The two are designed to coexist: `parked` must not preclude a later `archived` status, and both share the "a status the Available bucket excludes" read-site discipline.
- **Merging with `tracker-drop-verb`.** Decided to keep them separate (this feature can land first or independently).
- **DB-level status enforcement.** Status stays enforced in code, not a SQLite `CHECK` (a full table rebuild wasn't judged worth it). Adding `parked` is a one-line change to `FEATURE_STATUSES`, not a migration.
- **A dedicated park/release client.** Deferred (see Scope) — invoked via `curl` in the interim.
- **Renaming or reworking existing verbs.** `capture`/`claim`/`ship` keep their current behaviour; this feature only adds (and widens `claim`/`ship` source states).

## User stories

1. As a developer juggling several features
  I want to park a feature I've started but won't continue right now
  I claimed

  , got it partway, then decided to defer
      it. I park it — it leaves my In Progress list, shows up under Parked on the project page

  in the
      inbox's Parked category, and the owner is cleared — without me touching the database or pasting a banner into the
      doc.
2. As a developer who claimed too eagerly
  I want to release a claimed feature back to Available
  I claimed something this morning, then realised it's not the right thing to do this week. I
      release it: it returns to Available, the owner is cleared, and an event records that it happened — so someone else
      (or future me) can claim it cleanly.
3. As a developer resuming deferred work
  I want to pick a parked feature back up
  A feature has been sitting in Parked for a while; I see it in the inbox's Parked category. I
      claim it straight from there — it moves to In Progress with me as owner, no intermediate step — and work
      continues.
4. As a developer tidying the tracker
  I want to mark an already-shipped feature Done without claiming it first
  I notice a feature in Available that actually shipped weeks ago but was never tracked. I mark
      it Done in one step — it moves Available → Done and records a ship event — instead of faking a claim to
      satisfy the state machine.

## Data model

No new tables and no new columns. The changes are to existing data:

- **`features.status`** gains a fourth legal value, `parked`, joining `available`, `in_progress`, `done`. The set is held in `FEATURE_STATUSES` in code (no DB `CHECK` to change). `parked` is non-terminal and claimable.
- **`features.owner`** is cleared (set to `NULL`) by both `park` and `release` — neither leaves a stale owner on a feature nobody is working.
- **`events`** gains two new `event_type` values for history: `feature_parked` and `feature_released`, each a `document_id=NULL` row. Their payload records `{project, slug, owner}` — including the **owner being cleared**, so "who deferred / released this" survives even though the feature row no longer holds it (mirroring how `feature_claimed` records the owner). Resuming a parked feature reuses the existing `feature_claimed` event — there is deliberately *no* `feature_resumed` type; the `feature_parked`-then-`feature_claimed` sequence tells the story. Backfill reuses the existing `shipped` event — a backfilled feature is genuinely shipped.
- **Pre-existing data caveat.** One hand-made `feature_released` row already exists in the live DB (inserted manually during the originating incident), so analytics must not assume the type is brand-new. Tests are unaffected — they run against per-worker fresh databases.

**Bucket relationships.** `parked` is excluded from the Available bucket and from the inbox's In Progress and Recently-shipped categories — but it is *surfaced*, not dropped: its own bucket on the project page, its own category in the inbox, and a `## Parked` section in the exported `features.md`. This is the same "excluded-from-Available" discipline `tracker-drop-verb` needs, applied to a non-terminal status that stays visible.

## Technical approach

Follow the mutation contract established by `agent-submission-tracker-ops` and used by `claim`/`ship`, for every new transition:

- Redundant transition (already in the target state) → **idempotent no-op**, `changed=False`, *no* event.
- Invalid transition (wrong source state) → **409** via `InvalidTransition`.
- SSE broadcast **only** when the state actually changed. The broadcaster is a *contentless* ping (`broadcaster.broadcast()`); clients re-fetch. So there is no event *shape* to design — park/release/backfill reuse the existing notify-and-refetch path.
- An `events` row written for history on every real change.
- The status invariant lives in code, not a DB constraint.

**Transition matrix** (target ← legal sources):

- `park` → parked, from `{available, in_progress}`; clears owner; `parked → parked` is an idempotent no-op.
- `release` → available, from `{in_progress}`; clears owner. From `available` it's a no-op; from `done`/`parked` it's a 409.
- `claim` → in_progress, from `{available, parked}` (the second is "resume"); sets owner.
- `ship` → done, from `{in_progress, available}` (the second is "backfill"); shipping from `parked` is a **409** — resume it first, so the history stays honest. There is deliberately *no* `parked → available` edge: a parked feature is resumed by claiming it directly from the Parked bucket.

Each new transition gets a storage-layer mutation function and an HTTP route alongside the existing ones; the new `park`/`release` routes sit next to `claim`/`ship`.

**Read sites.** Every place that buckets by status must learn about `parked` — both to exclude it from Available and to surface it where it belongs:

- **Project page** — add a Parked group beside In Progress / Available / Done.
- **Inbox read-model** — its In Progress / Recently-shipped categories are allow-lists, so parked is excluded by construction; *add* a Parked category so deferred work surfaces on the cross-project attention page.
- **Single-feature page** (`web/feature_page.py`) — confirm it renders a parked feature sensibly (it reads feature status).
- **`features.md` exporter** — the `feature-html-to-md` tool (feature-skills repo) buckets features by status match (an allow-list), so an unknown `parked` status currently means a parked feature is *omitted* from the export entirely (not mislabelled as available — that leak doesn't apply here). Add a `## Parked` section so parked features appear in the committed snapshot.

**Visible-activity timestamps.** `list_features.last_activity` is derived from the feature's *documents'* events, not the `document_id=NULL` lifecycle events — so park/release (like `claim` today) deliberately do not bump the displayed activity time. This is intended, not an oversight.

## Testing

Tests assert observable state and history, not internal calls. For each new transition:

- **Happy path:** the feature ends in the target status, owner cleared where specified, and exactly the right event row is written with the expected payload — including the cleared owner in `feature_parked` / `feature_released`.
- **Idempotency:** repeating a transition that's already satisfied returns `changed=False` and writes *no* second event (incl. `park` from `parked`, `release` from `available`).
- **Invalid source → 409:** `park` from `done`; `release` from `done` or `parked`; `ship` from `parked`.
- **Broadcast discipline:** SSE broadcast fires on real change and not on a no-op.
- **Resume:** `claim` from `parked` sets the new owner, lands in `in_progress`, and emits `feature_claimed` (no distinct resume event).
- **Backfill:** `ship` from `available` lands in `done` and emits a `shipped` event (so the inbox Recently-shipped category picks it up).
- **Read-site behaviour (the regression guard):** a parked feature does *not* appear in the project page's Available list, nor in the inbox's In Progress / Recently-shipped categories; it *does* appear in the project-page Parked bucket and the inbox Parked category; and the single-feature page renders it.
- **Exporter (feature-skills repo):** a parked feature is emitted into the `## Parked` section of `features.md`, not dropped and not placed in Available.

Each behaviour gets a named test that fails without the change (per the project's test discipline). The read-site tests are the ones that catch the highest-risk behaviours: silent exclusion and silent omission.

## Alternatives

1. Backfill as a new verb (

  /

  ) instead of widening
  considered while scoping with user
  Rejected for now: a backfilled feature

  shipped, so reusing

  (with its source widened to accept

  ) keeps one verb. A separate verb could still emit the same

  event — it isn't forced to duplicate the event — but it would duplicate the done-transition
      logic and add API surface for a marginal gain in intent-clarity. Not worth it here.
2. Resume via a dedicated

  verb (parked → available) rather than
      claiming from parked
  considered while scoping with user
  Rejected: parked features are shown in their own bucket (project page + inbox) and are
      directly claimable, so

  from

  resumes work in one step. A separate
      unpark-to-Available verb adds a state hop with no clear use the Parked bucket doesn't already serve. Consequence: a
      parked feature never returns to

  — it goes to

  via claim or stays
      parked.
3. A distinct

  event for parked → in_progress
  raised in review round 1
  Rejected: resume reuses

  . The

  -then-

  sequence in the event log already distinguishes a
      resume from a fresh claim, so a new type would add a schema concept for no behavioural gain.
4. Name the in_progress → available reversal
  user decision
  chosen over

  — "release back to the pool" reads
      better than a negated verb, and it fits the capture/claim/ship vocabulary.
5. Park preserves the owner
  user decision
  Rejected: parked clears the owner — a parked feature has nobody on it, and resuming requires
      a fresh claim. Symmetric with

  . The prior owner is preserved in the event payload, not on the
      row.

## Delivery phases

### Phase 1 — Release transition

The smallest, fully independent slice and the one that most directly retires hand-editing. Add `release_feature` (in_progress → available, clears owner) in storage, a `release` route beside `claim`, the `feature_released` event (payload incl. cleared owner), and tests (happy path, idempotent no-op from available, 409 from done/parked, broadcast discipline). No new status, so no read-site changes. One MR.

### Phase 2 — Parked status + park + resume + read sites

The headline. Add `parked` to `FEATURE_STATUSES`; `park_feature` (available|in_progress → parked, clears owner, records prior owner in `feature_parked`) + route; widen `claim` to accept `parked` (resume, reusing `feature_claimed`). Update *all* read sites: project-page Parked bucket, a new inbox Parked category, and confirm `feature_page.py` renders parked. Includes the read-site exclusion *and* presence regression tests. One MR.

### Phase 3 — Backfill (available → done)

Widen `ship_feature`'s allowed source to include `available` (and reject `parked` with a 409); tests that a backfilled feature lands in Done, emits a `shipped` event, and surfaces in the inbox's Recently-shipped category. Small extension. One MR.

### Phase 4 — Exporter — `## Parked` section (feature-skills repo)

Cross-repo: teach `feature-html-to-md` about `parked` — add it to `_STATUS_SECTIONS` / `_SECTION_TO_STATUS` and emit a `## Parked` section in the merged `features.md`, so parked features appear in the committed snapshot rather than being silently omitted. Tested in the feature-skills repo. One MR there.

### Closeout — Dog-food correction

Not a code phase: once the above is live, park `review-severity-recalibration` via the new endpoint and remove its doc-body `PARKED` banner, so the originating feature is in the correct tracker state and the banner workaround is retired.

## Indicative notes

Plan-level detail worth carrying forward (not requirements constraints):

- **Files (webapp).** Storage mutations: `feature_skills_webapp/storage/tracker.py` (next to `claim_feature`/`ship_feature`; reuse `MutationResult`, `InvalidTransition`, `FeatureNotFound`; add `parked` to `FEATURE_STATUSES`). Routes: `feature_skills_webapp/web/tracker.py` handlers + registration in `feature_skills_webapp/web/app.py` (pattern: `/api/projects/{project}/features/{feature}/release` and `/park`). Read sites: `web/project_page.py` (add the `parked` list + `project.html` group), `storage/inbox.py` (add a Parked category + its `InboxCard` wiring), and `web/feature_page.py` (confirm parked renders).
- **Event payloads.** `INSERT INTO events (document_id, event_type, payload_json, created_at) VALUES (NULL, 'feature_parked'|'feature_released', json({project, slug, owner}), now)` — note `owner` is the value being cleared.
- **Ship signature.** Backfill keeps `ship_feature`'s optional `outcome`; only the source-state guard changes (`in_progress` → `{in_progress, available}`, with `parked` rejected).
- **FEATURE_STATUSES ordering.** `list_features` does `ORDER BY f.status, f.slug` (alphabetical); the project page groups explicitly, so adding `parked` needs no ordering rework.
- **Exporter (feature-skills repo).** `~/src/nigelmcnie/feature-skills/bin/feature-html-to-md` — `_STATUS_SECTIONS` (currently `{"In Progress", "Available", "Done"}`) and `_SECTION_TO_STATUS` drive the `--merge-features` bucketing; placement is by status match (an allow-list), so adding a `"Parked"` ↔ `"parked"` mapping + a `## Parked` section is the change.

## Design notes

Decisions captured from review round 1 (and the originating scoping):

- **Status is the single source of truth.** Parked is tracker state only; docs carry no park marker. New parking never touches doc bodies; the existing banner is retired in the closeout step. (round 1, item 1)
- **Parked work stays visible.** Not just excluded from Available — surfaced in a project-page bucket, an inbox Parked category, and a `## Parked` export section. The original failure was *invisibility*, so surfacing is a requirement, not a nicety. (round 1, items 4 & 5)
- **Exporter behaviour corrected.** The exporter buckets by status match (allow-list), so an unknown `parked` status causes *omission* from `features.md`, not re-surfacing as available. The fix (a `## Parked` section) is folded into this feature as Phase 4. (round 1, item 5)
- **No dedicated client yet.** park/release/backfill are invoked via `curl` for now; whether a first-class CLI/skill action is warranted is an open question to revisit — flagged, not resolved. (round 1, item 2)
- **No `parked → available` edge.** Resume is `claim` from the Parked bucket. (round 1, item 3)
- **Resume reuses `feature_claimed`**; no `feature_resumed` type. (round 1, item 8)
- **park/release clear owner but record it in the event payload.** (round 1, item 9)
- **Lifecycle events don't bump `last_activity`** — consistent with `claim`; intended. (round 1, item 6)
- **Ship from parked is a 409** — resume before shipping. (round 1, item 7)
- **Backfill is a scope addition** over the captured context, which listed only park + release; added with the user. (round 1, item 13)
- **One hand-made `feature_released` row already exists** in the live DB from the incident. (round 1, item 14)
