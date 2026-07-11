# feature-archive-semantics — Plan

## Overview

Today, retiring a feature from the tracker (the `drop` verb) throws away information: it flips the feature to `archived` and records nothing about *why* it was retired, *where the work went* if it went somewhere, and offers no way back. This plan replaces `drop` with a richer `archive` verb that captures a reason, an optional pointer to where the work went, and an optional note — and adds an `unarchive` verb so a hasty retirement can be undone. Archived features then render in a dedicated *Archived* section of the tracker so the record is visible.

The work is three independent merge requests. First, the API and a database migration (the whole capability, driven over HTTP). Second, showing the new metadata on the webapp's project page. Third, rendering an Archived section in the exported `features.md`. The two rendering MRs both depend on the first, because they can only display fields once the API returns them.

## Key decisions

- **Replace `drop` with `archive`; do not keep both**
  The tracker must have exactly one archival path. `drop` has no runtime callers (no skill invokes `/api/projects/{p}/features/{f}/drop`), so it is removed outright — route, handler, storage function, and its tests. The new verb requires a `reason`. Removing the advertised `/drop` route is a breaking change to `/openapi.json`; this is accepted.
- **Archival metadata is four nullable columns on `features`**
  A feature has one archival state, so no new table. DB columns are `archive_reason`, `superseded_by`, `archive_note`, `archived_at` (the `archive_` prefix avoids confusion with the existing `notes` column); the API field names are `reason`, `superseded_by`, `note` to match the sibling `document-archive-api` vocabulary.
- **Reason gates the superseded-by pointer**
  `subsumed`, `superseded`, and `duplicate` require a `superseded_by`; `obsolete` may stand alone. A request violating this is rejected 400 at the write boundary.
  ```
  ARCHIVE_REASONS = ("subsumed", "superseded", "duplicate", "obsolete")
  REASONS_REQUIRING_POINTER = ("subsumed", "superseded", "duplicate")
  ```
- **Superseded-by is free text, resolved best-effort with a soft warning**
  Stored verbatim (MR / decision refs are valid). If the value is slug-shaped (`slugify(v) == v`) and resolves to a real feature in the project, read surfaces link it. If it is slug-shaped but does *not* resolve, the archive still succeeds but the response carries a non-blocking `warning` so a typo'd slug is surfaced. Non-slug values (URLs, prose refs) are stored without resolution and without warning.
- **`unarchive` mirrors the existing verb transition matrix**
  archived → available (clears all four archival columns and the owner, emits `feature_unarchived`); already-available is a no-op (`changed=false`); any other status raises `InvalidTransition` → 409.
- **Re-archive is a no-op regardless of metadata**
  Archiving an already-archived feature returns `changed=false` and leaves the stored metadata untouched, even if the new request differs. Correcting a reason is `unarchive` then `archive`.
- **Archive retains owner; unarchive clears it**
  Archiving keeps the `owner` on the row (audit — who retired it, matching `drop`'s behaviour). `unarchive` returns the feature to `available`, which by convention has no owner (mirroring `release`), so it clears `owner`.
- **Migration number must be re-checked at merge time — guarded by a test**
  The next free migration is `0009`, but the sibling `document-archive-api` is being built in a parallel worktree and will also add a migration. Both cannot be 0009. This only surfaces when the second of the two merges to `main` — and the failure is *silent*: the migration runner skips any file whose number is `<=` the applied version (`storage/db.py`, `if version <= applied: continue`), so a duplicate-numbered migration that lands second never runs its `ALTER TABLE`s, leaving the columns missing with no error until a query fails. Two defences: (a) Phase 1 adds a **migration-uniqueness test** that asserts the `*.sql` stems form a unique, contiguous sequence whose max equals the expected schema version, so a clash fails CI loudly; and (b) a merge-to-main checklist checkpoint to renumber/re-run whichever lands second.
- **archive / unarchive are the first verbs to record `actor`**
  The `events.actor` column (migration 0008) exists so the inbox can distinguish agent- from human-driven activity, but no existing verb threads it — claim/park/release/ship all rely on the column's `DEFAULT 'agent'`. Because a feature is often archived by a human, `archive` and `unarchive` take an explicit `actor` parameter and write it into the event row. This is new plumbing, not a copy of the existing verbs.

## Data model

Migration `0009_feature_archive_metadata.sql` adds four nullable columns to `features` (mirroring the additive-`ADD COLUMN` style of `0008`; comments kept semicolon-free — the runner splits on `;`):

```
-- Semantic feature archival metadata. All nullable: populated on archive,
-- cleared on unarchive. Pre-existing archived rows (dropped before this
-- migration) legitimately carry NULL and must render as blank.
ALTER TABLE features ADD COLUMN archive_reason TEXT;
ALTER TABLE features ADD COLUMN superseded_by TEXT;
ALTER TABLE features ADD COLUMN archive_note TEXT;
ALTER TABLE features ADD COLUMN archived_at TEXT;

INSERT INTO schema_version (version) VALUES (9)
```

**Note:** the `9` above and the filename number are the values to re-check at merge-to-main against the sibling migration (see Key decisions). No backfill: the runner is forward-only, and additive nullable columns are low risk.

Events: `archive` emits `feature_archived` with payload `{project, slug, reason, superseded_by}`; `unarchive` emits `feature_unarchived` with `{project, slug}`. Both write the `actor` column explicitly from the request body (default `"agent"`) — the first verbs to do so (see Key decisions). The old `feature_dropped` type is no longer written; historic rows keep it (nothing reads it at runtime). `archived_at` is stored for the audit trail and used to order the Archived render (newest first).

## Contract

### POST /api/projects/{project}/features/{feature}/archive

Request body:

```
{
  "reason": "subsumed" | "superseded" | "duplicate" | "obsolete",   // required
  "superseded_by": "some-feature-slug" | "!123" | null,             // required unless reason=obsolete
  "note": "free text" | null,                                       // optional
  "actor": "agent" | "user"                                         // optional, default "agent"
}
```

Responses: **200** `{project, slug, status:"archived", changed, warning?}` — `warning` present only when a slug-shaped `superseded_by` did not resolve. **400** invalid JSON / non-object body / missing-or-unknown reason / missing pointer for a reason that requires one / wrong field types. **404** feature not found. **409** invalid transition (e.g. archiving a `done` feature). **503** db not configured.

### POST /api/projects/{project}/features/{feature}/unarchive

No request body required (optional `{actor}`). Responses: **200** `{project, slug, status:"available", changed}`; **404** feature not found; **409** invalid transition (not archived and not already available); **503** db not configured.

### Removed: POST .../drop

The `drop` route, handler, storage function, and OpenAPI entry are deleted. Its curated OpenAPI entry is replaced by `archive` + `unarchive` entries.

### Read endpoints gain the fields

`GET .../features` (listing) and `GET .../features/{feature}` each add `reason`, `superseded_by`, `note`, `archived_at` to every feature object (NULL when not archived).

## File structure

### Phase 1 — API + migration (feature-skills-webapp)

- `feature_skills_webapp/storage/migrations/0009_feature_archive_metadata.sql` — new
- `feature_skills_webapp/storage/tracker.py` — add `ARCHIVE_REASONS`, `archive_feature`, `unarchive_feature`, new error types, `warning` on `MutationResult`; extend `list_features` + `get_feature` SELECTs; remove `drop_feature`
- `feature_skills_webapp/web/tracker.py` — add `archive_handler`, `unarchive_handler`; add fields to `list_features_handler` + `get_feature_handler` JSON; remove `drop_handler`
- `feature_skills_webapp/web/app.py` — add archive/unarchive routes; remove drop route
- `feature_skills_webapp/web/openapi.py` — add archive/unarchive entries; remove drop entry
- `feature_skills_webapp/storage/tracker_test.py`, `web/tracker_test.py`, `web/openapi_test.py` — new tests; remove drop tests

### Phase 2 — Project-page rendering (feature-skills-webapp)

- `feature_skills_webapp/web/project_page.py` — `_feat()` carries archival fields for archived rows; resolve superseded_by to a sibling for linking
- `feature_skills_webapp/web/templates/project.html` — archived group shows reason / superseded-by (linked) / note, blank-tolerant
- `feature_skills_webapp/web/project_page_test.py` — assertions; switch `/drop` setup to `/archive`

### Phase 3 — Exported Archived section (feature-skills)

- `bin/feature-html-to-md` — add `Archived` to the section machinery and an Archived render branch in `_render_features_md`
- `tests/test_merge_features_md.py` — Archived-section tests

## Verification

Machine-runnable acceptance. Commands assume the repo root of each repo.

#### Phase 1 (feature-skills-webapp)

```
# Full suite + linters (per CLAUDE.md) — must be green
uv run ruff format --check .
uv run ruff check .
uv run ty check .
uv run pytest

# Live acceptance (attempt-first): apply the code, then
uv tool install --editable . --reinstall && systemctl --user restart feature-skills-webapp
# archive an obsolete (no pointer needed), confirm it took, then unarchive
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/<seed>/archive \
  -H 'content-type: application/json' -d '{"reason":"obsolete"}' > /tmp/arch.json
grep -q '"status":"archived"' /tmp/arch.json
curl -fsS http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/<seed> | grep -q '"reason":"obsolete"'
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/<seed>/unarchive > /tmp/un.json
grep -q '"status":"available"' /tmp/un.json
# drop route is gone: expect 404/405
curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/<seed>/drop  # 404 or 405
```

These curl checks target the deployed service and mutate the tracker, so run them against a throwaway seeded feature (or on a scratch DB); the assertions above are the behaviours proven by the named storage/web tests, which are the primary gate.

#### Phase 2 (feature-skills-webapp)

```
uv run pytest feature_skills_webapp/web/project_page_test.py
uv run pytest   # full suite
# after restart: an archived feature with a reason shows it on the project page
curl -fsS http://127.0.0.1:8800/project/feature-skills-webapp | grep -q 'feat-reason'
```

#### Phase 3 (feature-skills)

```
uv run pytest tests/test_merge_features_md.py
# render the tracker and confirm the Archived section appears
feature-html-to-md --webapp http://127.0.0.1:8800 --merge-features feature-skills-webapp /tmp/features.md
grep -q '^## Archived' /tmp/features.md
```

## Qc

Follow the QC steps in `feature-skills-webapp/CLAUDE.md` at implementation time (do not hardcode them here — read the file). At time of writing they are, run from the repo root, all must pass before committing:

```
uv run ruff format .      # or ruff format --check . in CI
uv run ruff check .
uv run ty check .
uv run pytest             # xdist + pytest-socket; per-worker DB
```

For the `feature-skills` repo (Phase 3), follow that repo's own QC conventions. After any code change to the deployed webapp, restart the service (`systemctl --user restart feature-skills-webapp`); after a dependency change, `uv tool install --editable . --reinstall` first — and never run `uv tool install` from a worktree (see CLAUDE.md).

## Checklist

### Phase 1: Archive / unarchive API + migration

- Add migration 0009_feature_archive_metadata.sql (four nullable columns).
- storage/tracker.py: add ARCHIVE_REASONS / REASONS_REQUIRING_POINTER; add warning field to MutationResult; add InvalidArchiveReason + MissingSupersededBy.
- storage/tracker.py: implement archive_feature (validation, gating, no-op, transition, resolve-warning, feature_archived event with explicit actor).
- storage/tracker.py: implement unarchive_feature (matrix, clears metadata + owner, feature_unarchived event with explicit actor).
- storage/tracker.py: implement _resolve_superseded_by_warning (slug-shaped + resolves check).
- storage/tracker.py: extend list_features + get_feature SELECTs with the four columns; remove drop_feature.
- web/tracker.py: add archive_handler (threads actor) + unarchive_handler (drop_handler tolerant-body pattern for optional actor); add the four fields to listing + single-feature JSON; remove drop_handler.
- web/app.py: add archive + unarchive routes; remove drop route.
- web/openapi.py: add archive + unarchive entries; remove drop entry.
- Write storage tests (archive/unarchive matrix, reason gating, warning paths, re-archive no-op incl. different-metadata, actor recorded, 404/409); remove drop storage tests.
- Write web tests (archive/unarchive 200/400/404/409, warning field, listing+single JSON fields); remove drop web tests.
- Update openapi_test: route set (archive/unarchive present, drop absent) AND _HIGH_VALUE_OPS_WITH_REQUEST_BODIES (drop out, archive in); confirm spec validates.
- Bump the schema-version assertions in storage/db_test.py (currently expect 8) to the new migration version.
- Add a migration-uniqueness test: *.sql stems form a unique, gap-free 1..N sequence with N == expected schema version (collision guard).
- Run full QC (ruff format/check, ty, pytest); run live curl acceptance after restart.
- MERGE CHECKPOINT: re-check highest migration on main; renumber the migration + schema_version + db_test/uniqueness expectations if the sibling landed one first; re-run suite. Open one MR.

### Phase 2: Project-page Archived rendering

- web/project_page.py: build archived entries with reason/superseded_by/note, sorted by archived_at (newest first); resolve superseded_by to a sibling slug for linking.
- web/templates/project.html: render reason / linked-or-text superseded-by / note in the archived group, all NULL-guarded.
- Write project_page tests (full metadata linked; non-resolving ref as text; NULL metadata renders clean); switch archived setup from /drop to /archive.
- Run full QC; curl the project page after restart and confirm the reason renders. Open one MR.

### Phase 3: Exported-tracker Archived section

- bin/feature-html-to-md: add 'Archived' to section order + status maps so archived rows are no longer dropped.
- bin/feature-html-to-md: add the 4-column Archived render branch in _render_features_md (rows sorted by archived_at newest-first, linked superseded-by, NULL-tolerant); leave the dead merge machinery untouched.
- Write test_merge_features_md tests (Archived section + columns, linked vs text superseded-by, NULL cells, ordering after Done, absent when none).
- Run the feature-skills QC; render the tracker and grep for '## Archived'. Open one MR.

## Phase 1

Delivers the whole capability over HTTP. All in `feature-skills-webapp`.

#### Migration

Add `0009_feature_archive_metadata.sql` as in Data model.

#### Storage (`storage/tracker.py`)

Add the enum constants, extend `MutationResult` with an optional warning, add two typed errors, and the two mutations. Remove `drop_feature`.

```
ARCHIVE_REASONS = ("subsumed", "superseded", "duplicate", "obsolete")
REASONS_REQUIRING_POINTER = ("subsumed", "superseded", "duplicate")

@dataclass(frozen=True)
class MutationResult:
    project: str
    slug: str
    status: str
    changed: bool
    warning: str | None = None          # new; None for all existing verbs

class InvalidArchiveReason(TrackerError): ...
class MissingSupersededBy(TrackerError): ...

def archive_feature(conn, *, project, slug, reason, superseded_by, note, actor, now) -> MutationResult:
    # 1. reason in ARCHIVE_REASONS else InvalidArchiveReason
    # 2. reason in REASONS_REQUIRING_POINTER and not superseded_by -> MissingSupersededBy
    # 3. get_feature; None -> FeatureNotFound
    # 4. status == 'archived' -> no-op MutationResult(changed=False)
    # 5. status not in ('available','in_progress') -> InvalidTransition (409)
    # 6. warning = _resolve_superseded_by_warning(conn, project, superseded_by)
    # 7. UPDATE features SET status='archived', archive_reason=?, superseded_by=?,
    #    archive_note=?, archived_at=? WHERE id=?   (owner retained)
    # 8. INSERT feature_archived event -- explicit actor column:
    #    INSERT INTO events (document_id, event_type, payload_json, actor, created_at)
    #      VALUES (NULL, 'feature_archived', ?, ?, ?)
    # 9. return MutationResult(project, slug, 'archived', changed=True, warning=warning)

def unarchive_feature(conn, *, project, slug, actor, now) -> MutationResult:
    # get_feature; None -> FeatureNotFound
    # status == 'available' -> no-op changed=False
    # status != 'archived' -> InvalidTransition (409)
    # UPDATE ... SET status='available', owner=NULL,
    #   archive_reason=NULL, superseded_by=NULL, archive_note=NULL, archived_at=NULL
    # INSERT feature_unarchived event (explicit actor column, as above)
    # return MutationResult(project, slug, 'available', changed=True)
```

`_resolve_superseded_by_warning`: return `None` if `superseded_by` is falsy or not slug-shaped (`slugify(v) != v`); else if `get_feature(conn, project, v)` is None, return a message like `"superseded_by 'x' does not resolve to a feature in this project"`; else `None`. Note the event `INSERT`s add the `actor` column explicitly — existing verbs omit it and take the `DEFAULT 'agent'`, so these two are the first to thread it (see Key decisions).

Extend the `list_features` (~L61) and `get_feature` (~L73) SELECTs to also select `archive_reason, superseded_by, archive_note, archived_at`.

#### Handlers (`web/tracker.py`)

`archive_handler`: parse body (must be object); `reason` must be a string in `ARCHIVE_REASONS` (else 400); `superseded_by` / `note` str-or-null (else 400); `actor` optional string. Call `archive_feature` (threading `actor`); map `InvalidArchiveReason` / `MissingSupersededBy` → 400, `FeatureNotFound` → 404, `InvalidTransition` → 409. Broadcast on `changed`. Return the result JSON including `warning` when non-null. `unarchive_handler`: follow `drop_handler`'s *tolerant-body* pattern (`raw = await request.body()`; parse only if non-empty) so an optional `{actor}` can be read — *not* `park_handler`, which parses no body at all; map errors 404/409. Add the four fields to `list_features_handler` / `get_feature_handler` JSON on each feature object. Remove `drop_handler`.

#### Routes + OpenAPI

`web/app.py`: add `archive` and `unarchive` POST routes next to the other verbs; remove the `drop` route. Import churn accordingly. `web/openapi.py`: replace the `drop` curated entry (~L431) with `archive` and `unarchive` entries following the `_lifecycle_responses` pattern; archive adds a request schema (reason enum + superseded_by/note) and `include_400=True`. Also update `web/openapi_test.py`'s `_HIGH_VALUE_OPS_WITH_REQUEST_BODIES` set (~L136), which lists `/drop` explicitly: remove drop, add `/archive` (it declares a request body); `/unarchive`'s body is optional so it need not join that set.

#### Tests

- **storage**: archive from available and from in_progress → archived (owner retained, `feature_archived` event with reason+superseded_by); each reason in `REASONS_REQUIRING_POINTER` without a pointer → `MissingSupersededBy`; `obsolete` without a pointer → ok; unknown reason → `InvalidArchiveReason`; superseded_by resolves → no warning; slug-shaped unresolved → warning; non-slug ref → no warning + no resolution attempt; re-archive already-archived (incl. with different metadata) → no-op, stored metadata unchanged; archive `done` → `InvalidTransition`; archive missing → `FeatureNotFound`. unarchive archived → available (metadata + owner cleared, `feature_unarchived` event); unarchive available → no-op; unarchive in_progress/parked/done → `InvalidTransition`; unarchive missing → `FeatureNotFound`.
- **web**: archive 200 (+ warning field present when unresolved); 400 (bad/missing reason, missing pointer, non-object body, non-string fields); 404; 409 (from done). unarchive 200/404/409. listing + single-feature JSON include the four fields (NULL when active, populated when archived).
- **openapi**: `archive` and `unarchive` appear in the curated set and `drop` does not; the `_HIGH_VALUE_OPS_WITH_REQUEST_BODIES` set is updated (drop out, archive in); `build_spec` still validates; the route-coverage and request-body/error-response tests pass.
- **schema version**: bump the schema-version assertions in `storage/db_test.py` (they currently expect `8` — e.g. `test_schema_version_after_migrate`, and the `migrate(conn) == 8` assertions) to the new version. Without this the full `pytest` gate fails.
- **migration uniqueness** (new, collision guard): a test asserting the migration `*.sql` stems parse to a unique, gap-free integer sequence `1..N` and that `N` equals the expected current schema version — so a duplicate/clashing migration number fails CI loudly instead of being silently skipped.
- Remove all `drop` tests.

#### Merge checkpoint

Before merging to `main`, re-run the migration-number check (see Key decisions): confirm no migration numbered `0009` already landed from the sibling; if one has, renumber this migration to the next free number, update its `schema_version` value, and re-run the suite.

One MR.

## Phase 2

Show the new metadata in the webapp's archived group. All in `feature-skills-webapp`; no schema change. Depends on Phase 1.

#### Handler (`web/project_page.py`)

The archived list needs its archival fields (the generic `_feat()` returns only slug/owner/last_activity). Build the archived entries with the extra fields and resolve `superseded_by` to a sibling for linking:

```
def _archived_feat(f, feats_by_slug):
    sb = f["superseded_by"]
    return {
        "slug": f["slug"], "owner": f["owner"],
        "reason": f["archive_reason"],
        "note": f["archive_note"],
        "superseded_by": sb,
        "superseded_by_slug": sb if (sb and sb in feats_by_slug) else None,
    }
```

`feats_by_slug` is built from the same `list_features` result. Sort the archived list by `archived_at` descending (newest first; NULL-metadata legacy rows last) before mapping, then pass `archived=[_archived_feat(f, by_slug) for f in archived_sorted]`.

#### Template (`web/templates/project.html`)

In the archived `{% for feat in archived %}` block, after the slug, render (all guarded, so NULL metadata shows nothing):

```
{% if feat.reason %}<span class="feat-reason">{{ feat.reason }}</span>{% endif %}
{% if feat.superseded_by_slug %}<a href="/project/{{ project|urlencode }}/feature/{{ feat.superseded_by_slug|urlencode }}">{{ feat.superseded_by }}</a>
{% elif feat.superseded_by %}<span>{{ feat.superseded_by }}</span>{% endif %}
{% if feat.note %}<span class="feat-note">{{ feat.note }}</span>{% endif %}
```

Reuse existing presentation classes where they exist; add minimal styling only if needed (grounded against the app's own CSS, not the doc contract).

#### Tests (`web/project_page_test.py`)

- Archived feature with full metadata → page contains the reason, the linked superseded-by (href to the sibling feature page), and the note.
- Archived feature whose `superseded_by` is a non-resolving ref → shown as text, not a link.
- Archived feature with NULL metadata (a legacy drop) → renders without error and without empty labels.
- Two archived features → ordered by `archived_at` newest-first.
- Switch existing archived-state setup from `POST /drop` to `POST /archive`.

**Note:** `project.html`'s all-empty guard (the `{% if not in_progress and not available and not parked and not done %}` around L220) omits `archived` — a project whose only features are archived would still show the empty state. Out of scope to change here, but the template is touched in this phase, so leave a comment rather than silently widening the condition.

One MR.

## Phase 3

Render a `## Archived` section in `features.md`. All in the `feature-skills` repo (`bin/feature-html-to-md`). Depends on Phase 1 (the listing API must return the fields). The live renderer is `_render_features_md` (~L549) — **not** the dead `_build_section_block` / `_STATUS_SECTIONS` merge machinery, which must be left untouched.

#### Changes in `_render_features_md`

- Add `"Archived"` to `_STATUS_SECTIONS_ORDER` (last, after Done) and `{"archived": "Archived"}` to `_SECTION_TO_STATUS` / `_STATUS_TO_SECTION`. This stops the `section is None → continue` from dropping archived rows.
- Add an Archived render branch (4 columns). Superseded-by links to the sibling's doc when it resolves to a known slug, else renders as text; NULL fields render as empty cells:

```
# inside the per-section loop, alongside the In Progress / Available / Done branches
elif h == "Archived":
    out.append("| Feature | Reason | Superseded by | Note |\n")
    out.append("|---|---|---|---|\n")
    slugs = {f["slug"] for f in db_features}
    feats = sorted(feats, key=lambda f: (f.get("archived_at") or ""), reverse=True)  # newest first
    for feat in feats:
        lc = _link_cell(feat["slug"], None)
        reason = (feat.get("reason") or "").replace("|", "\\|")
        sb = feat.get("superseded_by") or ""
        sb_cell = f"[{sb}](docs/features/{sb}/context.md)" if sb in slugs else sb.replace("|", "\\|")
        note = (feat.get("note") or "").replace("|", "\\|")
        out.append(f"| {lc} | {reason} | {sb_cell} | {note} |\n")
```

(Field names `reason`/`superseded_by`/`note` are the API field names Phase 1 returns.) The Archived section renders after Done; the `suggested_order` insertion after Available is unaffected.

#### Tests (`tests/test_merge_features_md.py`)

- A DB feature with `status="archived"` + full metadata → output has a `## Archived` section with the 4-column header and a row carrying reason, linked superseded-by, and note.
- Archived with a non-resolving superseded-by → text, not a link.
- Archived with NULL metadata → row renders with empty cells, no crash.
- Section ordering: `## Archived` appears after `## Done`; rows within it are ordered by `archived_at` newest-first.
- No archived features → no `## Archived` heading (existing empty-section behaviour).

One MR.
