# document-archive-api — Plan

## Overview

Right now, a document that was written through the webapp's API can never be retired. The only thing that can mark a document “archived” is the file-walker, and only when a source file on disk disappears — but API-authored documents have no file, so nothing can ever archive them. When the `writable-doc-types` migration left stale `requirements`-typed copies lying around, the only fix was to edit the SQLite database by hand.

This plan adds an **archive / unarchive** pair of API calls for individual API-authored documents. Archiving records *why* the document was retired (a `reason`), *where its content went* (an optional `superseded_by` pointer), and an optional free-text `note` — and it is reversible. Archived documents drop off the active lists (as they already do), but you can now list them, and opening one shows why it was retired and where its content went. A second, thin skill in the `feature-skills` repo makes an ad-hoc agent aware the capability exists so it routes to the endpoint instead of editing the database.

It is the document-level twin of `feature-archive-semantics`, which does the same for whole features. The two share the `reason` / `superseded_by` / `note` vocabulary; the document reason set is narrower.

## Key decisions

1. **Two verbs on the document logical-key URL, mirroring the tracker verbs**
  `POST …/{doc_type}/{instance}/archive` and `…/unarchive`, slotting in beside the existing `/comments` and `/synthesis` subpaths. Each is an idempotent storage mutation returning a small result with a `changed` flag, writes an event on real change, and broadcasts. This is the exact shape of `drop_handler` / `drop_feature`.
  ```
  POST /api/documents/{project}/{feature}/{doc_type}/{instance}/archive
  POST /api/documents/{project}/{feature}/{doc_type}/{instance}/unarchive
  ```
2. **API-authored documents only — reject file-sourced ones with 409**
  The verb refuses a document whose `source_path` is non-null. This is the load-bearing safety guard: the walker resets a file-sourced document's status to the file-derived value on the very next walk — even on an unchanged file (`walker.py:200-206`) — so an API archive of a file-sourced doc would silently revert. API-authored documents (`source_path IS NULL`) are never walked and are the entire intended target. Any doc-type is eligible; the only gate is the `source_path` guard.
3. **Reason is required and gates the superseded-by pointer**
  `reason` must be one of `superseded` / `duplicate` / `obsolete` (a subset of the feature sibling's enum — no `subsumed`). `superseded` and `duplicate` each require a `superseded_by`; `obsolete` may stand alone. A missing-required-pointer, an unknown reason, or a `superseded_by` that points at the document being archived is a 400. Beyond that `superseded_by` is free text (a document logical key, an MR, or a decision reference) — no resolution required.
  ```
  DOC_ARCHIVE_REASONS = frozenset({"superseded", "duplicate", "obsolete"})
  _REASONS_REQUIRING_POINTER = frozenset({"superseded", "duplicate"})
  ```
4. **Coined event types, not the walker's**
  Archive emits `document_archived`, unarchive `document_unarchived` — mirroring the sibling's coinage discipline rather than reusing the walker's `archived`/`reactivated` (which carry file-driven, content-change-only semantics). The coarse `events.actor` is set to `ACTOR_AGENT` (the inbox surfaces on the literal `'agent'`); the request body's finer `actor` string is recorded in the event `payload_json`, since these transitions cut no version row.
5. **Archival metadata as real, nullable columns on `documents`**
  Four nullable columns (`archive_reason`, `superseded_by`, `archive_note`, `archived_at`) via a new migration — not `metadata_json` — so the fields are queryable, which is what lets the listing enumerate archived documents and the doc-view render the reason. DB column names carry an `archive_` prefix; the API field names stay `reason` / `superseded_by` / `note` to match the shared vocabulary.
6. **Idempotent, reversible, fail-loud**
  Re-archiving an already-archived document is a no-op (`changed=false`), returning the existing metadata unchanged (correction is `unarchive` then re-archive). Archiving a missing document is a 404 (an explicit act; unlike `claim`'s best-effort skip). `unarchive` moves `archived → active` and clears all four columns; unarchiving an already-active document is a no-op.

## Data model

A new migration adds four nullable columns to `documents`, populated on archive and cleared on unarchive:

```
ALTER TABLE documents ADD COLUMN archive_reason TEXT;
ALTER TABLE documents ADD COLUMN superseded_by TEXT;
ALTER TABLE documents ADD COLUMN archive_note TEXT;
ALTER TABLE documents ADD COLUMN archived_at TEXT;
-- provisional number 0009 (next after main's 0008); reconcile at merge, no gap
-- (comments stay semicolon-free -- the runner splits on ';')
INSERT INTO schema_version (version) VALUES (9)
```

No new table and no new status — `archived` already exists end-to-end. The columns are NULL for every active document (and for the entire existing table after migration). Two new event types (`document_archived`, `document_unarchived`) join the existing vocabulary; the archived event's `payload_json` carries `{type, feature, reason, superseded_by, actor}`.

> **Migration-number merge gate (highest-risk step — read carefully).** Highest committed migration on main is `0008`; the sibling `feature-archive-semantics` also adds one. Both siblings build in *worktrees* off main, so both will naturally author `0009` and the collision only surfaces at merge. **Do not pre-assume an order and do not leave a gap.** Author this migration as the next *contiguous* number after main's highest — `0009` today. At merge to main, before pushing: set the number to `(highest-on-main + 1)` with *no gap below it* — so `0009` if we land first, `0010` if the sibling's `0009` is already there. A gap is a silent *production* hazard, not just a duplicate one: `migrate()` skips any file whose version is `<= ` the DB's applied version (`db.py:118`), so if the deployed DB has already reached `0010` and a `0009` hole is later filled, that `0009` is silently never run and its columns never get created — a 500 on the deployed instance that fresh test DBs never reproduce. Mirror `0008`'s style; semicolon-free comments; the footer's `schema_version` value must equal the file's number.

## Contract

### POST …/{doc_type}/{instance}/archive

Body (all fields optional except `reason`):

```
{"reason": "superseded", "superseded_by": "proj/feat/vision/1",
 "note": "content moved to the vision doc", "actor": "agent"}
```

Responses: **200** with the archival state; **400** unknown/missing reason, missing required `superseded_by`, or self-referential pointer; **404** document not found; **409** document is file-sourced (not archivable via the API).

```
200 -> {"logical_key": "...", "document_id": 42, "status": "archived",
        "changed": true, "reason": "superseded",
        "superseded_by": "proj/feat/vision/1", "note": "...",
        "archived_at": "2026-07-12T...Z"}
```

### POST …/{doc_type}/{instance}/unarchive

Body optional (`actor` only). Responses: **200** with `status:"active"` and cleared fields; **404** not found; **409** file-sourced. Re-unarchiving an active document returns `changed:false`.

### GET …/features/{feature}/documents?status=

New optional `status` query param: `active` (default, unchanged behaviour), `archived`, or `all`. Each returned document gains a `status` field, and archived documents additionally carry `reason` / `superseded_by` / `note` / `archived_at`.

### GET …/{doc_type}/{instance}

`get_document` gains `status` and (when archived) the four archival fields in its JSON response.

## File structure

### Phase 1 — API + migration (feature-skills-webapp)

- `feature_skills_webapp/storage/migrations/0009_document_archive.sql` — new; four nullable columns.
- `feature_skills_webapp/storage/documents.py` — `archive_document`, `unarchive_document`, `ArchiveResult`, `DocumentNotFound`, `ArchiveConflict`, reason constants.
- `feature_skills_webapp/storage/tracker.py` — `list_feature_documents` gains a `status` arg and returns the status + archival columns.
- `feature_skills_webapp/web/submit.py` — `archive_document_handler`, `unarchive_document_handler`; `get_document` returns archival fields.
- `feature_skills_webapp/web/tracker.py` — `list_documents_handler` reads `?status=` and emits the new fields.
- `feature_skills_webapp/web/app.py` — register the two new routes.
- `feature_skills_webapp/web/openapi.py` — `API_METADATA` entries (the `drop` entry at ~line 431 is the template) + the `status` query param on the documents-list entry.

### Phase 2 — Doc-view rendering (feature-skills-webapp)

- `feature_skills_webapp/web/doc_view.py` — `ROW_SQL` selects the archival columns; resolve `superseded_by` to a `/doc/{id}` link; pass an archived-notice context.
- `feature_skills_webapp/web/templates/doc.html` — render the archived notice (reason, linked superseded-by, note).
- `feature_skills_webapp/web/static/doc.css` — a small archived-notice rule if existing chrome classes don't fit.

### Phase 3 — Discovery skill (feature-skills repo)

- `~/src/nigelmcnie/feature-skills/<new-skill-dir>/SKILL.md` — thin pointer skill.
- Any skill-registration/index the repo keeps for its `feature-*` skills.

## Verification

All commands run from the `feature-skills-webapp` repo root. The full QC gate must pass (see QC).

```
uv run pytest                              # full suite (xdist); new modules must be green
uv run pytest feature_skills_webapp/storage/documents_test.py \
             feature_skills_webapp/storage/db_test.py \
             feature_skills_webapp/web/submit_test.py \
             feature_skills_webapp/web/tracker_test.py \
             feature_skills_webapp/web/openapi_test.py    # the new Phase-1 tests specifically
uv run ruff check . && uv run ty check . && uv run ruff format --check .
```

End-to-end smoke against a running instance (after `systemctl --user restart feature-skills-webapp` — a restart is manual per CLAUDE.md, since the deployed service runs the installed entrypoint, not the source):

```
# archive an API-authored doc, then confirm it enumerates and round-trips
WEBAPP=~/src/nigelmcnie/feature-skills/bin/webapp
printf '{"reason":"obsolete","note":"smoke"}' | \
  "$WEBAPP" post /api/documents/PROJ/FEAT/requirements/1/archive -   # -> status:"archived"
"$WEBAPP" get /api/projects/PROJ/features/FEAT/documents?status=archived  # -> lists it
"$WEBAPP" post /api/documents/PROJ/FEAT/requirements/1/unarchive -        # -> status:"active", changed:true
```

Phase 2: open an archived document's page and confirm the DOM shows the reason and a resolved superseded-by link — asserted by the `web/doc_view_test.py` cases named in Phase 2 (the authoritative check, since the live page needs the restart above).

## Qc

Run the project's full QC gate from `CLAUDE.md` before each MR — all must pass:

```
uv run ruff format .        # (CI: ruff format --check .)
uv run ruff check .
uv run ty check .
uv run pytest
```

The implementing agent follows whatever `CLAUDE.md` specifies at implementation time (this list may drift). New tests must observe behaviour, not mocks, and each new-behaviour test must be shown to fail without the change (per the repo's testing norms).

## Checklist

### Phase 1: Archive / unarchive API + migration

- Add migration `0009_document_archive.sql` (four nullable columns + schema_version footer); at merge, reconcile the number to the next contiguous integer with no gap (see the merge gate).
- Add reason constants, `ArchiveResult`, `DocumentNotFound`, `ArchiveConflict` to `storage/documents.py`.
- Implement `archive_document` (validation, file-sourced guard, idempotent no-op, event).
- Implement `unarchive_document` (round-trip, clears columns, no-op on active, event).
- Extend `list_feature_documents` with a `status` arg returning status + archival columns.
- Add `archive_document_handler` / `unarchive_document_handler` to `web/submit.py`; map errors to 400/404/409; broadcast on change.
- Return archival fields from `get_document`.
- Read `?status=` in `list_documents_handler` and emit the new fields (invalid status → 400).
- Register the two routes in `web/app.py`.
- Add OpenAPI metadata for both verbs (incl. `_FEATURE_PATH_PARAM_OVERRIDE` in `parameters`) + the `status` query param on the documents-list entry.
- Write the storage tests (reason matrix, guards, idempotency, round-trip, events).
- Write the web tests (status-code matrix, response shape, list filter, migration version, openapi).
- Run the full QC gate; open the Phase 1 MR.

### Phase 2: Doc-view Archived rendering

- Extend `ROW_SQL` in `doc_view.py` to select the archival columns.
- Resolve `superseded_by` to a `/doc/{id}` link when it matches a document; build the archived-notice context.
- Render the archived notice in `doc.html` (reason, linked superseded-by, note); add a CSS rule only if needed.
- Write `doc_view_test.py` cases (reason+note shown, link resolves, plain-text fallback, active doc shows nothing).
- Run the full QC gate; open the Phase 2 MR.

### Phase 3: Discovery skill (feature-skills repo)

- Create the thin pointer skill in `~/src/nigelmcnie/feature-skills` (SKILL.md: trigger + surfaced ops + /openapi.json + bin/webapp).
- Register it per the repo's skill conventions; run whatever lint/test that repo uses.
- Open the Phase 3 MR against feature-skills.

## Phase 1

Delivers the full archival capability end-to-end over the API. One MR, this repo.

### Storage — `storage/documents.py`

```
DOC_ARCHIVE_REASONS = frozenset({"superseded", "duplicate", "obsolete"})
_REASONS_REQUIRING_POINTER = frozenset({"superseded", "duplicate"})

class DocumentNotFound(Exception): ...      # -> 404
class ArchiveConflict(Exception): ...       # -> 409 (file-sourced / ineligible)
# reason/pointer/self-ref validation reuses SubmitError -> 400

@dataclass(frozen=True)
class ArchiveResult:
    document_id: int
    logical_key: str
    status: str            # "archived" | "active"
    changed: bool
    reason: str | None
    superseded_by: str | None
    note: str | None
    archived_at: str | None

def archive_document(conn, *, project, feature, doc_type, instance,
                     reason: str | None, superseded_by: str | None,
                     note: str | None, actor: str = "agent", now: str) -> ArchiveResult: ...

def unarchive_document(conn, *, project, feature, doc_type, instance,
                       actor: str = "agent", now: str) -> ArchiveResult: ...
```

`archive_document`: look up by `logical_key` selecting `id, status, source_path` + the archival columns → `DocumentNotFound` if absent. If `source_path` is not NULL → `ArchiveConflict` (409). Validate `reason` ∈ enum and the reason-gates-pointer rule and self-reference → `SubmitError` (400). If already `archived` → return `changed=False` with the existing metadata (no write). Else UPDATE status + the four columns, INSERT a `document_archived` event (`actor=ACTOR_AGENT`, payload carrying reason/superseded_by/request-actor), return `changed=True`. `unarchive_document`: same lookup + file-sourced guard; if not `archived` → no-op `changed=False`; else UPDATE status back to `active`, NULL the four columns, INSERT `document_unarchived`.

### Migration — `0009_document_archive.sql`

The four `ALTER TABLE … ADD COLUMN` statements + the `schema_version` footer (see Data model). Provisional number `0009`; **reconcile at merge per the merge gate — next contiguous number, no gap**. Mirror `0008`'s style; semicolon-free comments.

### Listing — `storage/tracker.py` + `web/tracker.py`

```
def list_feature_documents(conn, feature_id, *, status: str = "active"):
    # status in {"active", "archived", "all"}; "all" drops the status predicate.
    # SELECT now also returns d.status, d.archive_reason, d.superseded_by,
    #        d.archive_note, d.archived_at
```

`list_documents_handler` maps `?status=` to that arg (`active` default, `archived`, `all`; anything else → 400) and adds `status` plus the archival fields to each returned document.

### Handlers + routes — `web/submit.py`, `web/app.py`

Two handlers mirroring `drop_handler`: 503 when db unconfigured, parse the optional JSON body (reject a non-object body and non-string field values with 400), run the mutation inside `request_conn` + `transaction`, map `DocumentNotFound`→404 / `SubmitError`→400 / `ArchiveConflict`→409, broadcast when `changed`, return the archival JSON. Register both routes in `app.py` next to the other document subpaths. *Note:* `drop_handler` is the closest template, but the sibling may have removed `drop` by the time this rebases onto main — `claim`/`park`/`ship` (or the sibling's new `archive_handler`) are equally valid templates if so.

### OpenAPI — `web/openapi.py`

Add `API_METADATA` entries for both verbs (summary, requestBody example, the 200/400/404/409 responses). **Each entry must include `_FEATURE_PATH_PARAM_OVERRIDE` in its `parameters`** — every document-subpath op does, and `test_feature_sentinel_documented_on_every_document_path` (`openapi_test.py:182`) fails without it. Extend the documents-list entry with a `status` query parameter; the existing `_STATUS_PARAM` describes "feature status", so give the doc-list one its own `active|archived|all` description rather than reusing that verbatim.

### Tests

- **`storage/documents_test.py`** — archive happy path (each reason); missing-pointer rejection for `superseded` and `duplicate`; `obsolete` without a pointer accepted; unknown reason rejected; self-referential pointer rejected; file-sourced document rejected; 404 on missing; idempotent re-archive returns `changed=False` and does not overwrite metadata; unarchive round-trip clears all four columns; unarchive of an active doc is a no-op; the correct event type + payload + `actor='agent'` is written for each transition.
- **`web/submit_test.py`** — the HTTP status-code matrix (200/400/404/409) for both verbs; the response JSON shape; `get_document` returns the archival fields for an archived doc and nulls/omits them for an active one; a broadcast fires on a real change and not on a no-op.
- **`web/tracker_test.py`** — the documents list defaults to active (unchanged), `?status=archived` returns only archived with their archival fields, `?status=all` returns both, an invalid `status` is a 400.
- **`storage/db_test.py`** — a fresh migrate reaches version 10 and the four columns exist and are nullable.
- **`web/openapi_test.py`** — the spec exposes both new operations with their responses and the new query param.

## Phase 2

Show, on a viewed archived document, why it was retired and where its content went — beyond today's “(archived)” breadcrumb label. Read-model + presentation only, no schema change. One MR, this repo. Depends on Phase 1's columns.

### Read model — `web/doc_view.py`

Extend `ROW_SQL` to select `d.archive_reason, d.superseded_by, d.archive_note, d.archived_at`. When `status == 'archived'`, build an archived-notice context: the humanised reason, the note, and the `superseded_by` — resolved to a `/doc/{id}` link when it matches an existing document's `logical_key` (a single lookup), otherwise shown as plain text. Pass it into the template.

### Template — `web/templates/doc.html`

Render the notice near the top of the document region (a natural spot is beside the existing raw-fallback notice). Reuse an existing chrome class where one fits; add one small CSS rule to `doc.css` only if needed.

### Tests — `web/doc_view_test.py`

- An archived doc's shell renders the reason and the note.
- `superseded_by` that resolves to a real document renders a `/doc/{id}` link; an unresolvable pointer renders as plain text.
- An active doc renders no archived notice (negative space).

## Phase 3

A thin, feature-scoped skill that makes an ad-hoc agent aware the webapp API exists and points it at the self-describing spec — so “archive the old doc for feature X” routes to the Phase 1 endpoint. One MR, in the `~/src/nigelmcnie/feature-skills` repo (a different repo, as the sibling's exporter phase is) — this repo's export/commit steps do not apply to it.

### Skill content

Keep it a *pointer*, not a hand-maintained route mirror: it surfaces `/openapi.json`, the bundled `bin/webapp` helper, and the handful of common ad-hoc ops (read a feature's documents, list features, archive / unarchive a document — including `?status=archived` to find an archived doc to restore). A reasonable starting trigger (a session about features/docs in a repo with a `.feature-workflow.toml`, or that mentions the webapp) is fine — it is cheap to tune later.

### Verification

Follow the feature-skills repo's own conventions for skill files and whatever lightweight test/lint it runs; there is no webapp code in this phase.
