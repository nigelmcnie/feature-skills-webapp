# agent-submission-tracker-ops

## Problem

[agent-submission-api](../agent-submission-api/requirements.html) (F3) moved document authoring onto HTTP: any local agent now creates, updates, and reads back context / requirements / plan / feedback docs by logical identity, never by writing an HTML file or knowing a dev-store path. One part of the workflow's state was deliberately left behind — **the tracker**.

The tracker (a project's set of features, each with a status, owner, and notes) is still *file-shaped* at its only write path. To claim a feature, mark it shipped, or capture a new one, an agent must hand-edit the canonical `features.html` in the Claude dev-store — the exact standalone-HTML, dev-store-path coupling F1–F3 set out to retire, reintroduced for this one doc — and then wait for the filesystem walker to re-parse the file before the change shows up.

There is also no way to **enumerate** tracker state over the API. An agent (or a second agent like Codex) can write a doc by logical key, but cannot ask "what projects exist? what features are in this project? what documents does this feature have, and what are their logical keys?" — so it can't discover what to work on, or resolve a logical key it doesn't already hold.

These gaps block the in-flight [skills-api-cutover](../skills-api-cutover/requirements.html), which wants to retire the dev-store and walker entirely but can't while the tracker remains file-and-walker-bound. This feature supplies the missing write and read API for the tracker. It is scoped tightly to that additive substrate; the riskier walker surgery that makes the substrate authoritative is handed to skills-api-cutover (see Scope and Design notes).

## Vision

Any local agent claims, ships, and captures a feature — and enumerates the projects, features, and documents it can act on — over HTTP by logical identity, with no file to edit and no walk to wait on.

## Scope

This feature is the **additive API substrate** for the tracker: it is self-contained, shippable, and testable on its own, and it changes no existing walker or rendering behaviour. The end-state (the `features` table as the single source of truth) is reached *jointly* with skills-api-cutover, which owns the parts this feature cannot make safe alone.

**In scope:**

- **Typed tracker mutations** — `claim` (available → in_progress, set owner), `ship` (in_progress → done, record outcome), and `capture` (create an *available* feature) — by logical identity, writing the `features` table, emitting an event, and triggering an SSE refresh.
- **Listing** — read-only enumeration of projects, of a project's features (status / owner / notes), and of a feature's *active* documents (doc type, instance, logical key, version, inbox URL) over the API.
- **A status invariant** — a one-off backfill repairs existing `NULL`-status rows to `available`, and every create path sets a valid status, closing the existing "NULL-status feature is invisible" gap (held in code, not by a DB constraint).

**Out of scope — handed to [skills-api-cutover](../skills-api-cutover/requirements.html)** (captured in its context doc this round, so the work lands coherently across the two features):

- **The walker-authority flip** — retiring the walker's parse of `features.html` into the table. This is what makes the mutations *stick* (until then a re-walk re-derives rows from the file). It belongs with skills-api-cutover because it is only safe *after* the next item, which that feature owns.
- **Migrating the feature-* skills** (`/feature-context`, `/feature-requirements`, `/feature-plan`, the ship step) off editing `features.html` and onto these mutation endpoints — the precondition that makes the flip safe.
- **Repointing the repo `features.md` export** to regenerate from the DB (the export tool lives in the feature-skills repo, and skills-api-cutover already owns the export-from-DB move), and resolving the stale tracker-*document* view post-flip.

**Out of scope entirely (v1):**

- **"Move" / "reopen" / un-shipping.** Only the three transitions above; Nigel confirmed features are not un-shipped in practice.
- **Reordering within a state and the "suggested order" section.** Editorial, not modelled in the table; no ordering API.
- **Deleting / archiving features.** No current mechanism removes a feature row; v1 adds none (consistent with agent-submission-api leaving doc archival unsolved).
- **A typed MCP surface** and **auth.** HTTP is the contract; localhost / single-user trust is unchanged.

## User stories

1. As the requirements skill claiming a feature
  I want to move a feature into

  and set its owner over HTTP
  A developer runs

  . Instead of editing

  and running an export, the skill calls the claim mutation by project + feature slug; the table updates, the event fires, and open inbox tabs refresh live — no file write, no walk to wait on.
2. As an agent finishing a feature
  I want to mark a feature shipped with an outcome note
  At handoff, the agent calls the ship mutation. The feature moves to "Done", the outcome is recorded, and — because the same

  event fires — it surfaces in the inbox's "Recently shipped" list, exactly as a file-driven ship does today.
3. As an agent capturing an idea for later
  I want to add an

  feature to a project's tracker over HTTP
  A

  -style flow stashes an idea: it captures the feature (creating the tracker row, status

  , with a one-line note) and, separately, submits the context document via the existing

  — two clear write paths, one per concern.
4. As a second agent (e.g. Codex) deciding what to do
  I want to enumerate projects, features, and a feature's documents over the API
  A wrapper that has never seen the dev-store asks the API for the project's features, sees which are

  , picks one, and lists its documents to find the logical key of the context doc it needs to read — all without a filesystem path.

## Data model

No new tables. The existing `features` table (status, owner, notes per project + slug) is sufficient; what changes is that mutations write it directly, plus one constraint.

- **Feature identity is (project, slug).** A feature is addressed by its project name and slug — the same pair the walker already uses for its upsert — not by row id. Mutating an unseen project/feature auto-creates the rows, mirroring how a document submit already does.
- **Status is a small fixed set** — `available`, `in_progress`, `done` — kept valid at the application level, not by a DB constraint. A one-off backfill repairs existing `NULL`-status rows (which the shipped `upsert_feature` creates, and which fall out of every webapp status bucket) to `available`, and every write path that creates a feature sets a valid status. A hard `CHECK` was considered but rejected: SQLite can't add one without a full table rebuild, and the invariant is cheap to hold in code (a Postgres move someday could add the constraint then).
- **One `notes` column.** There is no separate "outcome" column — the walker already maps both the notes and outcome cells onto `notes` — so ship's outcome text writes `notes`; claim sets `owner`.
- **Mutation semantics are explicit.** Only `capture` creates a feature; `claim` and `ship` on a non-existent feature are a caller error (404), never a silent create (which would manufacture an invalid `NULL`-status row). An idempotent no-op (claiming an already-in_progress feature, re-shipping a done one) succeeds and emits no event, matching the walker's ship guard and retro-findings-capture's "no-op writes no event". An *invalid* transition (claiming a done feature, shipping an available one) is rejected (409), not silently swallowed.
- **Events.** Feature-level events carry `document_id = NULL` with a `{project, slug}` payload (the precedent set by the `shipped` event); ship reuses `shipped`, so the inbox's recently-shipped read needs no change.
- **The table is written additively, not yet exclusively.** Until skills-api-cutover retires the walker's parse, a re-walk re-derives rows from `features.html` and overwrites status/owner/notes (last-writer-wins) — and the skills still trigger walks today. So the mutations are safe to *test* and integrate, but **not yet safe to rely on against the deployed service**: a claim made via the API will be silently reverted by the next walk for any feature still present in `features.html`. This feature supplies the writes; the cutover makes them authoritative.
- **Concurrency** is the existing single-writer lock; resolution is last-writer-wins, consistent with the submission API.

## Technical approach

### Additive substrate, not a behaviour flip

This feature adds endpoints and one constraint; it does not change the walker. That keeps it self-contained and low-risk: nothing existing breaks because the walker parse still runs, and the mutations are immediately exercisable end-to-end (write the table, emit the event, broadcast the SSE) and testable in isolation — even though a subsequent walk will re-derive rows from the file until the cutover retires that parse. This mirrors how agent-submission-api shipped the document API as substrate before any skill consumed it.

### Typed mutations, not a generic field-setter

Expose claim / ship / capture as typed operations over HTTP, addressed by project + slug. Typed ops encode the valid transitions and select the right event (notably reusing `shipped` on the done-transition), which a generic "PATCH these columns" endpoint could not do without the caller re-implementing the rules. Capture is the only creating op; claim/ship 404 on a missing feature; a redundant transition is an idempotent no-op (no event); an invalid one is rejected (409) — see Data model for the full contract. Each mutation writes through the existing single-writer transaction and triggers the existing SSE broadcast, so open tabs update live — the same machinery document submits already use.

### Listing reads the table

Listing endpoints are thin read-models over the same queries the project and feature pages already run: projects by name; a project's features annotated by status, owner, notes; a feature's *active* documents (feature-scoped, so the project-level tracker doc never appears) with doc type, instance, logical key, current version, and `/doc/{id}` URL. Listing exposes only live (`active`) docs — never the transient `missing` state a re-walk can produce, nor (in v1) `archived` — so a consumer isn't handed flickering walker state. Read semantics mirror the submission API: 404 keys on the project/feature *row* existing, so a captured-but-undocumented feature returns an empty list, not 404; a genuinely unknown project/feature is 404; the DB-unconfigured case is 503.

### Why the authority flip lives in skills-api-cutover, not here

Making the table the single source of truth means retiring the walker's parse-into-table step — but the instant that parse stops, every consumer still editing `features.html` (the feature-* skills) silently stops affecting tracker state, with no error. This feature cannot guarantee the precondition that makes the flip safe — that those skills have migrated to these mutation endpoints — because that migration is skills-api-cutover's scope, and it is the feature that already deletes the walker. So the flip, the skill migration that gates it, the export-from-DB repoint, and the tracker-view cleanup all move there; this round wrote them into that feature's context doc so they fold in cleanly. The two features together achieve the whole tracker deliverable.

### Trust and locality

Unchanged from the submission API: localhost-only, single trusted user, no auth token. Mutations are state changes on a local SQLite file, no wider blast radius than the document writes already shipped.

## Alternatives considered

1. Do the walker-authority flip in this feature
  Source: original draft direction; round-1 review & decision with Nigel
  Rejected — moved to skills-api-cutover. This feature cannot enforce the flip's safety precondition (the feature-* skills must migrate to the mutation API first, or claims silently vanish), and that migration plus the walker deletion already belong to skills-api-cutover. Keeping the flip here would either strand the un-migrated skills or duplicate that feature's scope. Narrowing to the additive substrate contains the risk to the smallest workable boundary.
2. Each mutation rewrites the opaque

  body; walker stays authoritative
  Source: one of the two options named in the feature's tracker note; discussed with user
  Rejected. Keeping the file as source means a mutation must parse the opaque HTML, splice a row, re-serialise, and re-version it — a read-modify-write over presentation HTML, the exact "second write path" agent-submission-api refused for documents — and it leaves the race only narrowed, not removed. The end-state direction is the table as source (executed via the flip in skills-api-cutover).
3. Generic feature PATCH (set arbitrary status/owner/notes)
  Source: design tradeoff; round-1 review
  Rejected for v1. A free-form column setter pushes transition rules and event selection (e.g. emitting

  only on the done-transition) onto every caller. Typed claim/ship/capture keep that logic server-side and self-document the workflow.
4. Include a generic "move" and a "reopen" (done → in_progress)
  Source: round-1 review (the draft's undefined "move")
  Cut. "Move" had no defined transition; Nigel confirmed features are not un-shipped in practice, so reopen has no real use. The three concrete transitions (claim / ship / capture) cover the workflow; add more only on a demonstrated need.
5. Model row order and the "suggested order" list in the table now
  Source: design tradeoff (regeneration fidelity)
  Deferred. Adding an ordering column and a home for editorial prose is real schema and UI work for a need that isn't pressing. The export that would need them is skills-api-cutover's; that feature owns the fidelity call (its handoff record notes a merge-not-render approach — preserve the editorial regions rather than modelling them).

## Delivery phases

### Phase 1 — Listing over the API

Read-only enumeration: list projects; list a project's features with status / owner / notes; list a feature's documents with doc type, instance, logical key, current version, and inbox URL. Empty-vs-404-vs-503 semantics match the submission API. **Testable value:** an agent can discover projects, features, and documents — and resolve logical keys — entirely over HTTP. Read-only, lowest blast radius, useful immediately, fully independent of any walker change.

### Phase 2 — Typed mutations + status invariant

Claim / ship / capture write the `features` table directly with the explicit contract (capture creates; claim/ship 404 on missing; redundant transition = silent no-op; invalid transition = 409), emit the matching event (reusing `shipped` on the done-transition), and trigger an SSE refresh; capture sets `available`. A one-off backfill repairs existing `NULL`-status rows to `available` (a certainty on the live DB, not a contingency); the status vocabulary is held in code, no DB constraint. **Testable value:** an agent drives claim → ship and capture over HTTP; the table updates, events fire, and open tabs refresh — exercisable and testable in isolation. (Becoming the sole writer-of-record, safe against the live walker, arrives with skills-api-cutover's authority flip.)

## Indicative implementation notes

Plan-level seams worth carrying forward — concrete enough that the plan shouldn't re-derive them.

- **Reuse the feature upsert.** Mutations should write through the same `upsert_feature` / `upsert_project` path `storage/documents.py` uses, so API-created features and submit-created features converge on one row.
- **Events with `document_id = NULL`.** Feature-level events already exist in this shape (`shipped`, `{project, slug}` payload). New mutation events follow it; reuse the existing `shipped` event on the ship transition so the inbox's recently-shipped read is untouched.
- **Listing read-models already exist in spirit.** `web/project_page.py` and `web/feature_page.py` hold the exact queries (features-by-project with last-activity; documents-by-feature with awaiting flag, grouped by `DOC_TYPE_ORDER`). Factor the read accessors into `storage/` rather than duplicating the page handlers.
- **Status vocabulary needs a stable home.** Move only the value set `{available, in_progress, done}` to a stable location the mutations and any future renderer share — *not* from `walker.py`'s `_SECTION_STATUS` (retired by skills-api-cutover), and leaving behind its HTML-section-id mapping (`"in-progress" → "in_progress"`), which is parse-specific and dies with the walker.
- **The NULL-status backfill is a certainty, not a contingency.** Real `NULL`-status rows exist on the live DB today (the shipped `upsert_feature` creates them), so a one-off migration backfills them to `available`. No `CHECK` constraint (SQLite can't add one without a full table rebuild — rejected as not worth it); the vocabulary is held at the application level instead.
- **Validation / limits.** Mirror the pre-transaction validation, string-type checks, and size guards in `web/submit.py` / `web/synthesis.py` (e.g. reject non-string owner/notes with 400, as the submit handler now does for `actor`). Consider factoring the repeated `db_path is None → 503` guard rather than copying it again across the new handlers.
- **Redeploy discipline for verification.** The new endpoints only take effect after `systemctl --user restart` (code) / reinstall+restart (deps); test against the redeployed service, not the stale running one.
- **Anti-clobber keystone test lands with the flip.** The "a direct mutation survives a subsequent walk" test is meaningful only once the parse is retired, so it belongs in skills-api-cutover with the flip — noted here so it isn't lost. What this feature can pin: mutations write the expected rows/events, transition-gating emits nothing on no-ops, and capture yields an `available` (never NULL-status) row.

## Design notes

- **Narrowed to the additive substrate** (round 1, with Nigel): contain the risk to the smallest workable scope. This feature ships listing + mutations + the status invariant only; the walker-authority flip, the skill migration that gates it, the export-from-DB repoint, and the tracker-view cleanup move to skills-api-cutover, recorded in its context doc this round so they fold in cleanly. The end-state (table authoritative) is unchanged — only where the flip executes moves, to where its precondition can be enforced.
- **Mutation set is claim / ship / capture** (round 1, with Nigel): no generic "move" and no "reopen" — features aren't un-shipped in practice.
- **Capture creates the tracker row only** (round 1): the context document is a separate `PUT /api/documents` write — one write path per concern.
- **Status invariant held in code, not a DB CHECK** (round 2, with Nigel): a hard `CHECK` needs a full SQLite table rebuild, judged not worth it; instead a one-off backfill (`NULL → available`) plus every create path setting a valid status closes the invisibility gap. A future Postgres move could add the constraint then.
- **Mutation contract** (round 2): only `capture` creates; `claim`/`ship` 404 on a missing feature (never a silent NULL-status create); a redundant transition is an idempotent no-op with no event; an invalid transition is rejected with 409.
- **Listing exposes active docs only** (round 2): feature-scoped, excluding `missing` (transient walker state) and `archived`, so consumers aren't handed flickering state; 404 keys on the feature/project row, not on document presence.
- **Mutations test-safe, not service-safe until the flip** (round 2): the live walker reverts mutations on the next walk; explicitly flagged so "shippable on its own" isn't read as "usable in production on its own".
- **Mutations transition-gated** (round 1): no-op writes emit no event, mirroring the existing ship guard.
- **Export fidelity is merge-not-render** (round 1): owned by skills-api-cutover — regenerating `features.md` from the table must preserve unmodelled editorial content (row order, "suggested order" prose) by merging, not rendering from scratch.
