# document-archive-api — Requirements

## Summary

The `writable-doc-types` feature opened the API to *writing* bespoke document types, but nothing lets you *retire* a superseded API-authored document. A document's `archived` status is only ever set by the walker, and only when a source *file* disappears. API-authored documents have no source file, so no path — walker or API — ever archives them. The feature-level `drop` / `archive` verb retires a whole *feature*, not an individual document, so it can't retire just the stale copy when one document supersedes another within a live feature.

This bit us directly in the `writable-doc-types` Phase 3 migration: moving the ai-eng-planning north-star docs onto their natural `vision` / `system-map` types left the old `requirements`-typed originals stranded in each feature's active Documents list with now-stale content. The only way to retire them was a direct `UPDATE documents SET status='archived'` against the deployed SQLite database — outside the API entirely.

This feature closes that gap in two halves, shipped together. **(1) The setter:** an archive / unarchive endpoint pair so an API-authored document can be retired from the active list — carrying *why* it was retired (`reason`), *where the content went* (`superseded_by`), and an optional `note` — enumerated while archived, shown with its reason where it's read, and brought back, all without a direct database edit. **(2) Discoverability:** a thin feature-scoped skill that surfaces the webapp's `/openapi.json` and the bundled `bin/webapp` helper, so an ad-hoc agent asked to “archive the doc for wibbling the wobbles” routes to the new endpoint and gets it right first time. Without the second half the endpoint exists but nothing connects an ad-hoc request to it — so the two are one feature.

It is the document-layer sibling of `feature-archive-semantics`, which does the same for whole tracker rows. The two share the `reason` / `superseded_by` / `note` vocabulary; only the `reason` enum differs (narrower here — a single document is rarely *subsumed*).

## Vision

Retiring a superseded API-authored document is a single idempotent API call that records why it was retired and where its content went, stays findable and self-explaining while archived, is reversible if you got it wrong, and needs no direct database edit — the same self-explaining archive the feature layer gets, one level down.

## Non goals

- **Feature-level archival.** Retiring a whole tracker row (with all its documents) is the sibling feature `feature-archive-semantics`. This feature archives *individual documents*, and the two are orthogonal — archiving a feature never touches its documents' status, and archiving a document never touches its feature's status.
- **Archiving file-sourced documents.** The verb targets only *API-authored* documents (`source_path IS NULL`). A document that came from a file on disk is retired by deleting or moving that file — the walker owns its status, and (verified) would silently revert an API archive on the next walk. The verb rejects a file-sourced document at the boundary rather than letting that trap fire.
- **Hard delete.** Archiving is non-destructive — the document row and all its versions persist as an audit trail. There is no delete path; that is a deliberate constraint, matching the existing `archived` vocabulary.
- **Changing which lanes hide archived documents.** The inbox (`status = 'active'`) and the doc-view sibling nav already exclude archived documents; that behaviour is correct and unchanged. This feature *adds* a way to enumerate archived documents and renders their reason where a document is read — it does not alter the existing hide-from-lists behaviour.
- **A point-and-click archive button in the webapp.** Archival is driven over the API (by the feature-* skills, the discovery skill, and by hand). This feature adds the endpoint and the read surfaces, not an in-page control.
- **Reaching the walker-only `missing` status.** `missing` is set only by the walker when a source file disappears; API documents have no source file, so the API state space is a strict `active ⇄ archived` toggle.

## User stories

1. As a developer who moved a document's content onto a new doc type, I want to archive the stale original with a `superseded` reason and a pointer to the new document
  Scenario: after moving the ai-eng-planning north-star content onto its `vision` / `system-map` types, I archive the old `requirements`-typed document pointing at the `vision` doc — so the tracker records it was superseded and where its content lives, instead of me hand-editing the SQLite database.
2. As a developer whose document is simply out of date, I want to archive it as `obsolete` with a note and no superseded-by pointer
  Scenario: a scratch design doc that led nowhere gets archived as `obsolete` with the note “superseded by the plan discussion, no standalone doc” — and the write is accepted without a pointer, because nothing replaced it.
3. As a developer who archived a document too hastily, I want to list the archived documents, find the one I retired, and unarchive it
  Scenario: a document I archived turns out to still be current; I list the feature's archived documents over the API, find its logical key, and one `unarchive` call restores it to the active Documents list and clears the archival metadata — no database edit, and archive is never a dead end.
4. As anyone reading a retired document, to see, on the document itself, why it was archived and where its content went
  Scenario: opening an archived doc shows not just the “(archived)” label but “superseded by *[link to the vision doc]*” — so “why it's gone and where it went” is legible at a glance, matching what the feature sibling renders one level up.
5. As an agent driving the API over HTTP, a clear error when I archive with a reason that requires a superseded-by pointer but omit it
  Scenario: an attempt to archive as `superseded` with no `superseded_by` fails loudly with a 4xx at the write boundary, rather than silently storing an archive that claims a replacement exists but points nowhere.
6. As an ad-hoc agent that has never seen this webapp, to be told, when a session is about features, that the archive capability exists and how to call it
  Scenario: asked to “archive the old doc for feature X”, the discovery skill surfaces `/openapi.json` and `bin/webapp`, and I issue the correct `POST .../archive` first time instead of editing the database or giving up.

## Data model

A document has exactly one archival state, so the archival metadata lives inline on the existing `documents` row — four new nullable columns (mirroring the feature sibling's choice to put its archival columns on the `features` row) rather than a new table or the existing `metadata_json` blob. The columns are populated only when a document is archived and cleared when it is unarchived:

- **`reason`** — one of a fixed, narrow set naming why the document was retired: `superseded`, `duplicate`, `obsolete`. This is a subset of the feature sibling's enum (which also has `subsumed`); a single document is rarely absorbed into another the way a line of work is.
- **`superseded_by`** — free text pointing at where the content went, expected to be a document logical key (`project/feature/doc_type/instance`). Kept as free text, not a hard foreign key, so an MR or decision reference stays valid; read surfaces render it as a link when it resolves to a real document.
- **`note`** — optional free-text elaboration, the human sentence a future reader wants.
- **`archived_at`** — when the archival happened, for the audit trail and ordering.

Keeping the fields as real columns (not JSON) makes them queryable, which is what lets the read API *enumerate* archived documents and render each one's reason. An archival also writes an **event** — the audit spine every mutation already uses — of a newly coined type (`document_archived` / `document_unarchived`, mirroring the sibling's coinage rather than reusing the walker's file-driven `archived`/`reactivated`), carrying the `reason` and `superseded_by` in its payload. The `archived` status itself already exists end-to-end; this feature adds the metadata, the API transitions, and the events — not a new status.

## Technical approach

Follow the established verb pattern exactly: a `POST` to a verb subpath on the document's logical-key URL, an idempotent storage mutation returning a result with a `changed` flag, an event on real change, and an SSE broadcast. The endpoints slot in beside the existing document subpaths (`/comments`, `/synthesis`):

- `POST /api/documents/{project}/{feature}/{doc_type}/{instance}/archive`
- `POST /api/documents/{project}/{feature}/{doc_type}/{instance}/unarchive`

### API-authored documents only — reject the file-sourced walker trap

The verb archives only documents with a null `source_path`. A file-sourced document must not be archived through the API: the walker resets its status to the file-derived value on the next walk — even on an unchanged file — so an API archive would silently revert. The write boundary rejects a document with a non-null `source_path` with a 4xx, so the trap can't fire. API-authored documents (the entire intended target) have no source file and are genuinely walker-safe. Any existing API-authored document is eligible — no doc-type restriction beyond the `source_path` guard.

### Setter only — status stays orthogonal to content

The API content-write path (`PUT`, `submit_document` with no `source_path`) never touches status today, and must not start: a content `PUT` to an archived document does *not* reactivate it (decided with Nigel). Reactivation is only the explicit `unarchive`. This keeps the archive verb the single lever for status, mirroring the sibling's “one archival path” discipline.

### Reason gates superseded-by

`superseded` and `duplicate` each name a retirement whose content went somewhere, so a `superseded_by` pointer is required for them; `obsolete` may stand alone. The write boundary rejects a missing-pointer-for-those-reasons request with a 4xx rather than storing an orphan archive, and also rejects a `superseded_by` that points at the document being archived (an obvious self-referential nonsense). Beyond that it is free text — no resolution is required.

### Fail loudly on a wrong key

Archiving a document that does not exist returns 404 — archival is an explicit act and a wrong logical key should surface, not no-op (matching the sibling; unlike `claim`, whose skip-silently is a best-effort convenience).

### Idempotent, reversible toggle

Re-archiving an already-archived document is a no-op returning `changed=false` (not a 4xx). `unarchive` moves `archived → active` and clears the archival metadata. Because `unarchive` always exists, archive is never a dead end.

### Read API: expose and enumerate

The single-document read endpoint returns the archival fields, and the document *listing* gains a way to surface archived documents (today it hard-filters `status = 'active'`, so an archived document is invisible to the API and the “find it to unarchive” story has no path). Enumerating archived documents plus rendering their reason is what makes the archive self-explaining rather than a silent disappearance.

### Render where a document is read

The doc-view already labels a viewed archived document “(archived)”; this feature enriches that to show the `reason`, the `superseded_by` (linked when it resolves to a real document), and the `note` — the document-level analogue of the sibling's project-page Archived rendering.

### Discovery skill

A thin feature-scoped skill in the `feature-skills` repo that loads when a session is about features and points at `/openapi.json`, the `bin/webapp` helper, and the handful of common ad-hoc operations (read a feature's documents, list features, archive / unarchive a document). It is a pointer to the self-describing spec, not a hand-maintained mirror of every route — the effect of a discovered MCP toolset without MCP, which MintMCP cannot provide for a localhost server anyway. A reasonable starting trigger is fine; it is cheap to tune later.

## Alternatives

1. Store the archival metadata in `metadata_json` This draft; resolved in review round 1
  Rejected in favour of real columns. It would avoid a migration, but the `documents` table already uses `metadata_json` for `title`/`size` (risking key collisions) and it puts the fields off the query path — which the enumerate-and-render surfaces now depend on. Columns win for parity with the sibling and queryability.
2. Reuse the walker's `archived` / `reactivated` events This draft; resolved in review round 1
  Rejected in favour of coining `document_archived` / `document_unarchived`. The walker's `reactivated` connotes “a missing file returned” and its `archived` is emitted only on content change — reusing them attaches the wrong emission semantics to a pure status flip and muddies the audit log. Coinage matches the sibling's discipline.
3. A `status` field on the logical-key `PUT` Context open question, 2026-07-12
  Rejected — overloads the content-replacement path with a status side effect, exactly the coupling the “PUT does not reactivate” decision avoids. A dedicated verb keeps status changes explicit and separately authorised.
4. `POST /doc/{id}/archive` keyed by numeric id Context open question, 2026-07-12
  Rejected — the numeric `/doc/{id}` is the human HTML view; the API is keyed by `{project}/{feature}/{doc_type}/{instance}`, and the verb belongs on that logical-key surface next to `/comments` and `/synthesis`.
5. Hard foreign key for `superseded_by` Sibling requirements
  Rejected for the same reason as the sibling — a superseder is not always a tracked document (it may be an MR or a decision reference). Free text with best-effort link rendering keeps both cases working.

## Delivery phases

### Phase 1 — Archive / unarchive API + migration (webapp)

Migration `0010` (the four nullable archival columns on `documents`); the archive / unarchive storage mutations — `reason` validation, the reason-gates-superseded-by rule, the self-referential-pointer rejection, the file-sourced (`source_path IS NOT NULL`) guard, idempotent no-op on re-archive, 404 on a missing document, the reverse `archived → active` clearing metadata, and a coined `document_archived` / `document_unarchived` event + broadcast on real change; their `POST` endpoints; exposure of the archival fields on the single-document read endpoint *and* a way to enumerate archived documents in the listing; and the `/openapi.json` entries. Cover the validation matrix (each reason with and without a pointer, self-referential pointer, file-sourced rejection, idempotency, 404, the round-trip unarchive) with tests. On its own this delivers the full archival capability end-to-end over the API. One MR, this repo.

### Phase 2 — Doc-view Archived rendering (webapp)

Enrich the doc-view so a viewed archived document shows its `reason`, `superseded_by` (linked when it resolves to a sibling document), and `note`, beyond today's “(archived)” label — the document-level analogue of the sibling's project-page rendering. Read-model and presentation only; no schema change. One MR, this repo.

### Phase 3 — Discovery skill (feature-skills)

A new thin feature-scoped skill in the `feature-skills` repo that surfaces `/openapi.json`, the `bin/webapp` helper, and the common ad-hoc operations so an ad-hoc agent routes an archive request to the Phase 1 endpoint. This lives in a different repo (as the sibling's exporter phase does), so its MR and the workflow's export/commit steps target `feature-skills`, not this repo. One MR, sibling repo.

## Indicative notes

Plan-level detail worth carrying forward (not binding requirements):

- **Touch points.** `storage/documents.py` (the archive/unarchive mutations, alongside `submit_document`), `storage/tracker.py` (`list_feature_documents`, for the archived-enumeration path), `web/submit.py` (handlers), `web/app.py` (routes), `web/openapi.py` (spec entries — the existing document subpaths are the template), `web/doc_view.py` (Phase 2 rendering), and the exporter/skill in the `feature-skills` repo (Phase 3).
- **Migration number is a merge-time gate.** Highest current is `0008`; the sibling `feature-archive-semantics` claims `0009`, so this feature's columns are `0010`. Both siblings are implemented in *worktrees*, so the collision won't show up until merge to main — the plan must make “on merge to main, double-check the migration numbers don't collide with whatever the sibling landed, and renumber/fix if they do” an explicit step at that point (the runner sorts migration files and splits on `;`, so a duplicate number is a silent hazard). Mirror the existing `ALTER TABLE … ADD COLUMN` style and the semicolon-free comment convention.
- **Column vs API naming.** DB columns likely `archive_reason` / `superseded_by` / `archive_note` / `archived_at` (avoiding collision with any existing column semantics), while the API field names stay `reason` / `superseded_by` / `note` to match the sibling's shared vocabulary. DB name vs API field may differ.
- **Event actor.** Set the coarse `events.actor` to `ACTOR_AGENT` (matching every other document event; the inbox surfaces on the literal `'agent'`). The archive/unarchive cut no version row, so the request body's finer `actor` string can only be recorded in the event `payload_json`, alongside `reason`/`superseded_by` — not in `events.actor`.
- **Result type.** The verb returns a document-shaped result (logical_key, status, changed, and the archival fields) — it fits neither the tracker's `MutationResult` nor the write path's `SubmitResult`; name a small dedicated result.
- **Idempotency wrinkle.** Re-archiving an already-archived document with *different* metadata: the safe default is no-op (`changed=false`), correction via `unarchive` + re-archive — matching the sibling's stance.
- **Read-model reuse.** No new exclusion logic is needed — `inbox.py` (`status = 'active'`) and `doc_view.py` sibling nav already filter archived documents out; the new read work is additive (enumerate + render).

## Design notes

Positions taken, with the review round that settled each (workshop 2026-07-12; requirements review round 1):

- **Verb subpath on the logical key** (`/archive`, `/unarchive`), not a `PUT` status field or a numeric-id route — workshop.
- **API-authored documents only** — reject file-sourced (`source_path IS NOT NULL`) documents, because the walker would silently revert the archive on the next walk (verified against `walker.py`). Round 1.
- **Coined events** `document_archived` / `document_unarchived` rather than reusing the walker's `archived`/`reactivated` — matches the sibling's coinage discipline and keeps the append-only audit log unambiguous. Round 1.
- **Storage as real columns** on `documents` (not `metadata_json`) — for parity with the sibling and because enumerate + render depend on the fields being queryable. Round 1.
- **Enumerate archived documents over the API and render the reason on the doc-view** — both in v1 scope, so an archive is findable and self-explaining, not a silent disappearance. Round 1.
- **Shared vocabulary with `feature-archive-semantics`** (`reason` / `superseded_by` / `note` + `archived_at`); only the reason enum differs — narrower here, keeping `superseded` / `duplicate` / `obsolete` and dropping `subsumed`. Round 1 confirmed the full three-value set.
- **Reason-gates-superseded-by** (`superseded` / `duplicate` require the pointer, `obsolete` optional), plus a self-referential-pointer rejection — the no-orphan / no-nonsense-archive guardrail. Workshop + round 1.
- **Free-text `superseded_by`** with best-effort link rendering, not a hard FK — so document logical keys, MR links, and decision references all stay valid.
- **Content `PUT` does not reactivate** an archived document (decided with Nigel); 404 on a missing document; idempotent no-op on re-archive; feature-status and document-status archival are orthogonal. Workshop + round 1.
- **Thin discovery skill**, bundled with the endpoint (Phase 3, feature-skills repo); a reasonable starting trigger is fine and cheap to tune later. Round 1.
