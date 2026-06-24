# tracker-feature-notes-update — Requirements

## Summary

In plain terms: every feature row in the tracker carries a one-line **note** — the short description shown next to the feature in the inbox. Today there is **no way to change that note once it exists**. You can set it when you first `capture` a feature, but after that nothing can touch it: `capture` refuses (409) on an existing feature, and `claim`/`ship` don't edit notes.

This feature adds a small endpoint to **update an existing feature's note**. Example: a feature captured months ago as *"spike — maybe drop"* is now real work; one call rewrites its note to *"in progress, see PR #42"* and the inbox reflects it immediately.

It also closes a related trap. Authoring a context document before capturing a feature silently creates the feature row with an *empty* note, after which `capture` can never fill it in. The new endpoint gives an escape hatch, and a small skill reordering stops the trap recurring.

## Problem

There is no way to update an existing feature's one-line tracker note. Each existing mutation verb refuses the job:

- `capture` is create-only — it returns **409** on an existing slug and never touches the note of a feature that already exists.
- `claim`, `park`, `release` and `ship` change status (and `ship` can set an outcome note), but none offers a plain "edit the note" operation.

Worse, the document `PUT` (`submit_document`) **auto-creates the feature row** via `upsert_feature` with a *NULL* note and `status='available'`. So the common skill ordering — author a `context` doc, then `capture` — strands the feature: the `PUT` creates the row first, `capture` then sees it already exists and 409s, and the note passed to `capture` is silently discarded.

Hit directly while capturing the kea backlog (2026-06-24): `llm-judge-check` ended up with an empty note this way, and stale notes on `ast-query-dsl` and `required-pattern-realiser` could not be refreshed.

## Scope

### In scope

- A new API operation — `POST /api/projects/{project}/features/{slug}/note` with body `{"notes": "…"}` — to set the `notes` field on an *existing* feature (`web/tracker.py` + `storage/tracker.py`).
- Idempotent behaviour: re-sending the same note is a no-op (no event, no broadcast); 404 if the feature does not exist; 400 on a missing or non-string note; event + live SSE broadcast when the note actually changes.
- Allowed from *any* status; the update never changes the feature's status — so a shipped feature's outcome can be reworded while it stays `done`.
- Test coverage for the new mutation and handler.

### Adjacent — separate repo, must land in lockstep

- Reordering the `feature-context` skill to `capture` *before* its first document `PUT`, so the auto-create trap cannot recur; and teaching the capture/refresh skills to call the new endpoint when a note is stale. These live in the skills repo, not here — tracked as a dependent follow-up (and recorded as a risk; see Delivery phases).

## Vision

Any existing feature's one-line note can be rewritten with a single API call — and the document-PUT-before-capture trap can no longer strand a feature with an unfixable empty note.

## Non goals

- **Treating a note edit as inbox activity.** A note update bumps `updated_at` but deliberately does *not* move the feature's `last_activity` / inbox recency (see Design notes).
- **Editing the owner** through this endpoint. Owner belongs to the lifecycle verbs (`claim` sets it, `park`/`release` clear it); a back-door owner edit would bypass the status machine.
- **Changing a feature's status.** The note update only touches the note; status transitions stay with their own verbs.
- **Making `capture` idempotent.** `capture` stays a clean create that 409s on an existing slug (see Alternatives).
- **Changing `submit_document`'s auto-create.** The walker import path relies on `upsert_feature`; the ordering is fixed in the skill instead (see Alternatives).
- **A general multi-field metadata PATCH.** Only the note is editable; nothing else needs it today.

## User stories

1. As an agent capturing a backlog, I want to refresh a feature's stale note, e.g. `ast-query-dsl`'s note no longer matches the plan, so I send the new one-liner and the inbox updates — no need to drop and re-capture the feature.
2. As an agent that authored a context doc first, I want to set the note on a feature whose row already exists, e.g. `llm-judge-check` was stranded with an empty note; one call fills it in rather than leaving it blank forever.
3. As the owner of a shipped feature, I want to refine its recorded outcome wording after the fact, e.g. tightening the one-liner or adding a PR link to a shipped outcome — the note updates and the feature stays `done`, never silently un-shipped.
4. As a developer reading the inbox, I want notes to reflect current reality, when a note changes, the SSE broadcast refreshes the open tracker view immediately, the same as every other mutation.

## Data model

No schema change. The `features` table already has a `notes` column; this operation writes to it and bumps `updated_at` (which is not itself surfaced in any listing). A new event type (suggested: `feature_note_updated`) is recorded in the existing `events` table, matching how `capture`/`claim`/`ship` log their mutations.

## Technical approach

Follow the established tracker-mutation shape exactly — the same one `capture`, `claim`, `park`, `release`, `ship` and `drop` use.

### Endpoint

`POST /api/projects/{project}/features/{slug}/note` with body `{"notes": "…"}`. POST (not `PATCH`) keeps it consistent with the existing action-style verbs, and a single-purpose `/note` resists drifting into the multi-field metadata PATCH that *non-goals* rejects. The `notes` key must be present and a string; an empty string clears the note; a missing key or non-string value is a **400**.

### Storage mutation

A typed mutation in `storage/tracker.py` looks up the feature, raises `FeatureNotFound` if absent, and otherwise updates the note. It returns a `MutationResult` and is idempotent: if the stored note already equals the requested note it returns `changed=False` and emits no event (mirroring `claim`'s already-in-state short-circuit). On a real change it updates `notes` and `updated_at` and records a distinct event (suggested name `feature_note_updated`).

### Status applicability

A note update is allowed from *any* status and **never changes the feature's status**. This intentionally permits rewording a shipped (`done`) feature's outcome — which `ship` stores in the same `notes` column — while the feature stays `done`.

### Handler behaviour

- **Broadcast:** on a change the handler fires the shared broadcaster, which is a content-free "something changed" ping — clients re-fetch; the note value itself is not pushed.
- **DB-only:** the operation writes only to the database. It does not round-trip to the dev-store files, and the walker's `upsert_feature` never updates `notes` on existing rows, so a later import won't clobber an API-set note.
- **Concurrency:** the read-modify-write is last-writer-wins, consistent with the sibling mutations and bounded by the per-request transaction.

## Testing

Mirror the existing tracker tests (storage-level mutation tests plus handler tests). Spend the coverage on the edges, not just the happy path:

- Updating an existing feature's note changes it and returns `changed=True`; the inbox broadcast fires.
- Re-sending the identical note is idempotent: `changed=False`, no event, no broadcast.
- Updating the note on a `done` feature changes the note and leaves the status `done` (the reword-a-shipped-outcome case).
- Targeting a non-existent feature returns 404.
- A missing or non-string note body returns 400; an empty-string note is accepted and clears the note.
- Setting the note on a feature that was auto-created with a NULL note (the stranded case) fills it in.
- Each new test is confirmed to fail without the change, per the project's test discipline.

## Alternatives

1. Make `capture` idempotent — update notes on an existing slug instead of 409Context doc, option (b)Rejected as the primary contract: it overloads a create verb with edit semantics, would silently overwrite a `ship` outcome note, and breaks the skills' "treat 409 as success" contract. A dedicated verb keeps `capture`'s meaning clean.
2. Stop the document `PUT` auto-creating the feature rowContext doc, option (c)Rejected: `upsert_feature` is shared with the walker/import path, which legitimately materialises features from files on disk. Removing auto-create would break import or force the PUT to reject uncaptured features. The skill ordering fix removes the trap without touching this path.
3. Have the document `PUT` carry notes into the auto-created rowContext doc, option (c), second variantRejected: it couples document content to tracker metadata and only helps the create-from-`PUT` path, not general note edits. The dedicated verb plus the skill reorder is cleaner and covers every case.
4. `PATCH` on the feature resource instead of a `/note` actionREST conventionReasonable and arguably cleaner, but the codebase has no singular-feature GET and every existing mutation is an action-style `POST`; a `/note` POST is more consistent and a tighter single purpose. Decided in favour of POST.

## Delivery phases

### Phase 1 — Note-update endpoint

The whole in-repo deliverable, as one MR: the storage mutation, the HTTP handler, the route registration, and tests. Ships a working, idempotent note-update operation with 404/400 handling and live broadcast.

### Follow-up · separate repo — Skill lockstep

In the skills repo (not this one): reorder `feature-context` to `capture` before its first document `PUT` so the auto-create trap cannot recur, and teach the capture/refresh skills to call the new endpoint when a note is stale. Must land in lockstep with Phase 1's contract.

**Recorded risk:** footgun *prevention* lives entirely in this cross-repo change — Phase 1 provides only *recovery* (the note endpoint), not prevention — so if the skill reorder slips or regresses, the stranding trap can recur. A cheap in-repo belt-and-braces (a distinguishable event when `submit_document` auto-creates a feature) was considered and deliberately skipped as gold-plating.

## Indicative notes

- Suggested event type: `feature_note_updated` (parallels `feature_captured`, `feature_claimed`, `feature_dropped`).
- Suggested storage signature: `update_feature_note(conn, *, project, slug, notes, now) -> MutationResult`, alongside the other mutations in `storage/tracker.py`.
- Empty-string note is a legal value: it clears the note. A missing key or non-string is rejected (400).
- Once shipped, backfill the real-world casualties: set `llm-judge-check`'s empty note and refresh `ast-query-dsl` / `required-pattern-realiser`.

## Design notes

- **Endpoint locked (review round 1):** `POST /api/projects/{project}/features/{slug}/note`, body `{"notes": string}` — POST over PATCH for consistency with the existing action verbs. The `notes` key is required and must be a string; an empty string clears the note; a missing key or non-string is a 400.
- **Any-status, status-preserving (round 1):** note edits are allowed from any status and never change the status. This is intentional so a shipped outcome can be reworded (e.g. tightening the one-liner, adding a link) while the feature stays `done`.
- **Not inbox activity (round 1):** note edits bump `updated_at` but deliberately do not move `last_activity` — a note tweak is not treated as feature activity for inbox recency.
- **Prevention deferred to the skill (round 1):** no in-repo belt-and-braces. The stranding-footgun prevention relies on the lockstep `feature-context` reorder; this is recorded as a risk (Phase 1 delivers recovery only).
