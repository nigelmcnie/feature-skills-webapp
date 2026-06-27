# api-coherence — Plan

## Overview

Tidy the agent-submission + tracker + export surface into one coherent resource model before the MCP facade wraps it. Projects and features become explicit, strictly-created resources; documents (and the disk importer) require their parents to exist; the database becomes canonical and the `features.md` export renders from it; the feature listing is prepared for search; and the contract changes are signposted so concurrent or mid-flight agents recover gracefully. Eight independently-shippable phases (0–7), each one MR, spanning this repo and the feature-skills repo (the export tool + the `feature-context` skill). Trust model unchanged: localhost, single-user, no auth.

## Key decisions

### Symmetric create verbs; capture retired

Feature creation moves to `POST /api/projects/{p}/features/{f}`, mirroring `POST /api/projects/{p}` for projects — one symmetric strict create-with-notes door per resource (409 if it exists). The existing `capture_feature` (`storage/tracker.py:90`) is renamed `create_feature`; the `POST .../features/{f}/capture` route and `capture_handler` are retired; the `feature_captured` event is renamed `feature_created` (no code consumes the event string — it is only written — so historical rows simply keep the old type). Its one API call site is the `feature-context` skill (migrated in Phase 2); the implementer should grep the skills repo for `/capture` call sites and prose. Notes are edited via the existing idempotent `update_feature_note` (`:265`).

```
# storage/tracker.py
def create_feature(conn, *, project: str, slug: str, notes: str | None, now: str) -> MutationResult:
    # strict: raise FeatureExists if the row exists; else INSERT + 'feature_created' event
    # (renamed from capture_feature; its upsert_project call is removed in Phase 3)

```

### New exceptions + strict project create

```
# storage/tracker.py
class ProjectNotFound(TrackerError): ...
class ProjectExists(TrackerError): ...

def create_project(conn, *, name: str, now: str) -> MutationResult:
    # strict: raise ProjectExists if the row exists; else INSERT + 'project_created' event
def get_project_row(conn, name: str) -> sqlite3.Row | None:  # name/repo_path/suggested_order
# NB: distinct from the existing get_project (tracker.py:23), which selects only id/name.
# get_project_row selects repo_path + suggested_order for the single-project GET.

```

### Existence checks replace upsert in the API write path

`submit_document` (`storage/documents.py:130`) currently calls `upsert_project`/`upsert_feature` (`:151-152`). These become *lookups* that raise when the parent is absent:

```
# storage/documents.py (inside submit_document)
project_id = require_project(conn, project)          # raise ProjectNotFound if None
feature_id = require_feature(conn, project_id, feature) if feature is not None else None
                                                     # raise FeatureNotFound if None

```

The `upsert_project`/`upsert_feature` bodies (`walker.py:162,170`) stay, but their *only* caller becomes the walker (the bulk importer). The running service never auto-creates a parent.

### Self-explaining error bodies (the load-bearing hint)

```
# web — shared message helpers, introduced in Phase 0
def missing_feature_msg(project, feature):
    return (f"feature '{feature}' does not exist in project '{project}'. "
            f"Create it first: POST /api/projects/{project}/features/{feature}")  # the create verb, not /capture
def missing_project_msg(project):
    return (f"project '{project}' does not exist. "
            f"Create it explicitly first: POST /api/projects/{project}")

```

### notices channel

```
# web/submit.py — static transition list, cleared in a later cleanup
_NOTICES = ["api-coherence in progress: document writes will soon require the feature "
            "(and project) to exist — create them first."]
# included in get_manifest(), list_features_handler(), list_projects_handler() responses

```

### One write path for the importer (W3) — a real refactor, mind the import cycle

`walker._process_file` (`walker.py:183`) declares parents from disk (`upsert_project`/`upsert_feature`, dependency order) then delegates the doc body to `submit_document`; the disk tree is the explicit bulk declaration, and a path `identity_for` can't resolve is skipped (as today). Two cautions for the implementer:

- **Import cycle.** `documents.py:18` imports `logical_key`/`upsert_feature`/`upsert_project` *from* `walker.py`; making `walker` call `submit_document` would close a cycle. Relocate those helpers to a new `storage/parents.py` (and keep `walker` re-exporting for back-compat, or repoint *every* importer). The full importer set is wider than just `documents.py`: `logical_key` is also imported from `walker` by `web/submit.py:19`, `storage/tracker.py:9` (co-imported with `slugify` on the same line — **split that import**, `slugify` stays in `walker`), and the doc/tracker test modules; `upsert_*` also by the test modules. Repoint or re-export all of them.
- **Not a thin deletion.** `_process_file`/`walk` do more than `submit_document`: `source_path`/`source_mtime`, archived-vs-active derivation, and `reactivated`/`archived`/`missing` events with mtime/size gating. So `submit_document` must grow optional importer parameters (`source_path`, `source_mtime`, status, `actor="importer"`, the archived/reactivated branch) *or* the walker keeps a post-submit step for those — the convergence “same rows” claim must allow for the importer setting `source_path`/`actor` the API leaves null.

### Suggested order = project free-text column

```
# migration 0007_project_suggested_order.sql
ALTER TABLE projects ADD COLUMN suggested_order TEXT;
INSERT INTO schema_version (version) VALUES (7)

```

Set via a new write endpoint; surfaced on the single-project GET; rendered verbatim by the export as the `## Suggested order` section between Available and Done.

### Export renders from the DB (cross-repo)

In feature-skills `bin/feature-html-to-md`, replace `_merge_features_md` (line 548) and its preservation helpers with a render-from-DB function: fetch the features listing + the single-project GET (for `suggested_order`), render the status sections in DB order, and emit the Suggested-order section verbatim. Idempotent on its own output.

## Data model

No new entities, no new user data, trust boundary unchanged. Changes:

| Table | Change |
|---|---|
| `projects` | **+ `suggested_order TEXT`** (migration 0007, nullable). Explicit create only; `upsert_project` no longer called by the API path. |
| `features` | No schema change. Created strictly via `capture` (carries notes); no longer seeded by document writes. `created_at` already present (optionally surfaced in the export). |
| `documents` | No schema change. Writes require feature + project to exist; one write implementation shared with the importer. |

Relationships unchanged (document → feature → project); only the creation rule changes — one explicit door per resource, no implicit parent creation in the running service. Suggested order is project-scoped free text rendered as the `## Suggested order` section in its canonical position between Available and Done.

## Contract

New and changed HTTP endpoints (error body is always `{"error": "..."}`; 503 if DB unconfigured):

| Method & path | Phase | Behaviour |
|---|---|---|
| `GET /api/manifests/{doc_type}` | 0 | Response gains a `notices` array (static transition message). |
| `GET /api/projects/{p}/features` | 0, 6 | Gains `notices` (P0); gains `?q=` (text over slug/notes) and `?status=` filters (P6). |
| `POST /api/projects/{p}/features/{f}` | 1, 3 | **New.** Strict create-with-notes (409 if the feature exists); replaces `capture`. From P3 it 404s if the project doesn't exist (stops upserting it). |
| `GET /api/projects/{p}/features/{f}` | 1 | **New.** Single-feature read → `{project, slug, status, owner, notes}`; 404 if unknown. |
| `POST /api/projects/{p}/features/{f}/capture` | 2 | **Retired.** Route + `capture_handler` removed once `feature-context` migrates to the create verb. |
| `PUT /api/documents/{p}/{f}/{type}/{n}` | 2, 3 | **Changed.** 404 (self-explaining body) if the feature (P2) or project (P3) doesn't exist; no longer seeds either. |
| `POST /api/projects/{p}` | 3 | **New.** Explicit strict project create; 409 if it exists. |
| `GET /api/projects/{p}` | 3, 5 | **New.** Single-project read → `{name, repo_path, suggested_order}` (`suggested_order` lands in P5); 404 if unknown. |
| `PUT /api/projects/{p}/suggested-order` | 5 | **New.** Set the free-text Suggested order (body `{"text": "..."}`). |

Comments/synthesis (P7): no endpoint change; doc-id is documented as the canonical write key.

## File structure

Paths below are shorthand; the real package paths are `feature_skills_webapp/storage/…` and `feature_skills_webapp/web/…`.

### This repo — feature-skills-webapp

- `storage/tracker.py` — rename `capture_feature`→`create_feature` (+ `feature_created` event); new exceptions, `create_project`, `get_project_row`, `require_*` helpers, `list_features` filters, suggested-order accessor/mutation
- `storage/parents.py` (new) — relocated `logical_key`/`upsert_project`/`upsert_feature` so both `documents.py` and `walker.py` import from here (breaks the P4 cycle)
- `storage/documents.py` — replace parent upserts with existence checks; grow optional importer params for the W3 fold
- `storage/walker.py` — `_process_file` delegates the doc body to `submit_document`; declares parents from disk via the relocated helpers
- `storage/migrations/0007_project_suggested_order.sql` — new column
- `web/tracker.py` — `create_feature_handler` (new POST), `get_feature_handler`, `create_project_handler`, `get_project_handler`, `set_suggested_order_handler`, listing filters, `notices`; remove `capture_handler`
- `web/submit.py` — 404 mapping + self-explaining bodies; `_NOTICES`; manifest `notices`; shared message helpers
- `web/app.py` — register the new routes
- `docs/transitions/api-coherence.md` — repo transition note (P0; P7 addendum)
- `*_test.py` alongside each module

### feature-skills repo

- `bin/feature-html-to-md` — replace merge-preservation with render-from-DB
- `tests/test_merge_features_md.py` — rewritten for the renderer
- `feature-context/SKILL.md` — reorder capture-before-PUT; resumption on “already exists”

## Verification

Run from the repo root unless noted. The full pytest suite is the primary gate (CLAUDE.md: xdist + pytest-socket, per-worker DB).

### Whole-suite + static checks (every phase)

```
uv run ruff format --check .
uv run ruff check .
uv run ty check .
uv run pytest          # the real suite — must be green

```

### HTTP contract probes (service running the merged code)

After restarting the service (`systemctl --user restart feature-skills-webapp`):

```
# P0: manifest carries the transition notice
curl -fsS http://127.0.0.1:8800/api/manifests/requirements | grep -q '"notices"'

# P1: create verb (strict) + single-feature read
curl -fsS -o /dev/null -w '%{http_code}' -X POST \
  http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/tmp-feat \
  -H 'Content-Type: application/json' -d '{"notes":"x"}'   # → 200 then 409 on repeat
curl -fsS http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/api-coherence \
  | grep -q '"status"'

# P2: capture retired; document write to an unknown feature 404s with a self-explaining body
curl -fsS -o /dev/null -w '%{http_code}' -X POST \
  http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/x/capture   # → 404/405 (gone)
curl -fsS -o /dev/null -w '%{http_code}' -X PUT \
  http://127.0.0.1:8800/api/documents/feature-skills-webapp/no-such-feat/requirements/1 \
  -H 'Content-Type: application/json' -d '{"sections":{"summary":"x"}}'   # → 404

# P3: explicit project create is strict; unknown project read 404s
curl -fsS -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8800/api/projects/tmp-proj   # → 200 then 409
curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/api/projects/no-such-proj        # → 404

# P5: suggested order round-trips
curl -fsS -X PUT http://127.0.0.1:8800/api/projects/feature-skills-webapp/suggested-order \
  -H 'Content-Type: application/json' -d '{"text":"1. foo"}'
curl -fsS http://127.0.0.1:8800/api/projects/feature-skills-webapp | grep -q 'suggested_order'

# P6: listing filters
curl -fsS 'http://127.0.0.1:8800/api/projects/feature-skills-webapp/features?status=in_progress' \
  | grep -q '"features"'

```

### Cross-repo export (P5)

```
cd /home/nigel/src/nigelmcnie/feature-skills
uv run pytest tests/test_merge_features_md.py     # rewritten renderer tests (idempotence etc.)
# render → render byte-identical against unchanged DB state:
feature-html-to-md --webapp http://127.0.0.1:8800 --merge-features feature-skills-webapp /tmp/a.md
feature-html-to-md --webapp http://127.0.0.1:8800 --merge-features feature-skills-webapp /tmp/b.md
diff /tmp/a.md /tmp/b.md     # → no differences

```

## Qc

Follow whatever `CLAUDE.md` specifies at implementation time. Currently the QC gate (all must pass before committing) is:

```
uv run ruff format .
uv run ruff check .
uv run ty check .
uv run pytest

```

Phase 5 adds a migration but no dependency change, so a plain `systemctl --user restart feature-skills-webapp` suffices to run the new code against the live service (no `uv tool install` needed — and never run that from a worktree, per CLAUDE.md). The feature-skills export change is verified in that repo's own suite.

## Checklist

### Phase 0: Signpost

- Add `_NOTICES` and include it in the manifest, features-listing, and projects-listing responses.
- Add `missing_feature_msg`/`missing_project_msg` helpers.
- Write `docs/transitions/api-coherence.md`.
- Tests: notices present on the three endpoints; no behaviour change. MR 1.

### Phase 1: Feature create + read verbs

- Rename `capture_feature`→`create_feature` (event `feature_created`); keep a thin `capture_feature` alias for now.
- Add `create_feature_handler` + route `POST /api/projects/{p}/features/{f}` (capture route stays).
- Add `get_feature_handler` + route `GET /api/projects/{p}/features/{f}`.
- Tests: POST creates→200 then 409; GET existing→fields, unknown→404, unknown project→404; capture alias still green. MR 2.

### Phase 2: Documents require their feature; migrate skill; retire capture

- Replace `upsert_feature` in `submit_document` with a lookup that raises `FeatureNotFound`.
- Map it to 404 + `missing_feature_msg` in `put_document`.
- Migrate `feature-context` to the create verb; reorder create-first; resumption on 409.
- Retire `capture`: remove route, `capture_handler`, alias, capture tests; grep skills for residual `/capture`.
- Tests: unknown-feature 404, existing succeeds, no row seeded, convergence holds, capture route gone, 409-resumption path. MR 3 (lockstep).

### Phase 3: Explicit projects

- Add `ProjectExists`/`ProjectNotFound`, `create_project`, `get_project_row`.
- Remove `upsert_project` from `create_feature` and `submit_document`; raise `ProjectNotFound`.
- Add `POST`/`GET /api/projects/{p}` handlers + routes + error mapping.
- Tests: create 200→409, read 200/404, child ops under unknown project 404, `feature=None`+missing-project 404; update churned tests. MR 4.

### Phase 4: One write path (W3)

- Relocate `logical_key`/`upsert_*` to `storage/parents.py`; repoint `documents.py`/`walker.py` imports (break the cycle).
- Grow `submit_document` with optional importer params (source_path/mtime/status/actor + archived-reactivated branch), or keep a post-submit step in the walker.
- Refactor `_process_file` to declare parents from disk then delegate the doc body to `submit_document`.
- Tests: empty-DB bootstrap, idempotent re-import, reconcile, unresolved path skipped. MR 5.

### Phase 5: Suggested order + export from DB

- Migration 0007 (projects.suggested_order); accessor + `PUT .../suggested-order`; surface on single-project GET; add `created_at` to the features listing.
- Rewrite export to render-from-DB (deterministic, status sections in DB order; Suggested-order verbatim between Available and Done; archived excluded; keep `--merge-features` name + comment).
- Tests: webapp round-trip + migration; export idempotence, DB-only notes appear, suggested-order slot, archived excluded. MR 6 + 6b.

### Phase 6: Listing search prep

- Add `q`/`status` params to `list_features` + handler.
- Tests: by text, by status, combined, empty, no-params. MR 7.

### Phase 7: Addressing (docs)

- Document doc-id as the canonical comments/synthesis write key in the transition note. MR 8 (or fold in).

## Phase 0

**Signpost the coming changes — additive, webapp-only, ships first.**

### Built

- A static `_NOTICES` list (transition message) in `web/submit.py`, added to the `get_manifest` response, and to `list_features_handler` / `list_projects_handler` (the latter reach the human UI + `feature-choice`, not authoring agents — that's expected).
- Shared message helpers (`missing_feature_msg`, `missing_project_msg`) that Phases 2–3 use for the actionable error bodies.
- `docs/transitions/api-coherence.md` — a short human/agent-readable note of the coming contract changes.

### Files

`web/submit.py`, `web/tracker.py`, `docs/transitions/api-coherence.md`.

### Tests

- Manifest response includes the populated `notices` array.
- Features/projects listing responses include `notices`.
- No existing behaviour changes (existing tracker/submit tests stay green).

**MR 1.**

## Phase 1

**Feature resource verbs — create + read; webapp-only, additive.**

### Built

- `POST /api/projects/{project}/features/{feature}` → `create_feature_handler` calling `create_feature` (renamed from `capture_feature`; strict create-with-notes, 409 if it exists, emits `feature_created`). Symmetric with the project create in Phase 3.
- `GET /api/projects/{project}/features/{feature}` → `get_feature_handler` wrapping the existing `get_feature` accessor; returns `{project, slug, status, owner, notes}` or 404.
- The old `capture` route stays in place (unchanged) so `feature-context` keeps working until its Phase-2 migration — this phase is purely additive.
- Register `GET` and `POST` on the bare `.../features/{feature}` path as **two separate `Route(..., methods=[...])` entries** (the codebase already does this for `/retro-findings`); a method-less `Route` defaults to GET-only and would 405 the create. No path collision with the sibling `.../capture` or `.../documents` routes (extra segment) or the existing `GET .../features` listing (no `{feature}`).

### Files

`storage/tracker.py` (rename + keep `capture_feature` as a thin alias for now), `web/tracker.py` (handlers), `web/app.py` (routes).

### Tests

- POST creates a feature with notes (200); a second POST → 409.
- GET an existing feature returns the four fields; unknown feature → 404; unknown project → 404.
- The existing `capture` tests still pass (alias).

**MR 2.**

## Phase 2

**Documents require their feature; migrate the skill; retire capture — lockstep webapp + skills.**

### Built

- `submit_document` stops calling `upsert_feature`; instead looks up the feature and raises `FeatureNotFound` when absent. (Project upsert stays until P3.)
- `put_document` catches `FeatureNotFound` → 404 with `missing_feature_msg`.
- `feature-context` migrated to the new create verb and reordered to **create-first, then PUT**; on an “already exists” (409) response to the create it treats it as *resumption* — fetch the feature (single-feature GET from P1), continue if benign (still available / its own), refresh notes via the note operation if needed, and surface only a genuine collision.
- **Retire `capture`**: now its last caller has migrated, remove the `POST .../capture` route, `capture_handler`, the `capture_feature` alias, and the `capture`-specific tests. Grep the skills repo to confirm no other `/capture` call site remains; update any prose mentions.

### Files

`storage/documents.py`, `storage/tracker.py` (drop alias), `web/submit.py`, `web/tracker.py`, `web/app.py` (drop route); feature-skills `feature-context/SKILL.md`.

### Tests

- Document write to an unknown feature → 404; the message names the create endpoint.
- Write to an existing feature still succeeds (create then write).
- After the change, a document write creates *no* feature row (assert the feature is absent / row count unchanged).
- The convergence test (file-import ↔ API) still holds.
- The retired `capture` route is gone (404/405); the migrated create path is exercised.
- Update the one event-string test that asserts `feature_captured` (`storage/tracker_test.py:347`) to expect `feature_created`.

**MR 3** (webapp) — must land together with the `feature-context` skill change. The skill change is net-new behaviour (verb switch + reorder + resumption), so verify the reordered flow end-to-end (including a re-run that hits the 409 resumption path) before/at merge.

## Phase 3

**Explicit projects, no implicit creation — webapp (+ light skills note).**

### Built

- `create_project` (strict) + `ProjectExists`/`ProjectNotFound`; `get_project_row`. Symmetric with `create_feature`.
- Remove `upsert_project` from `create_feature` and from `submit_document`; both raise `ProjectNotFound` when the project is absent.
- Endpoints: `POST /api/projects/{p}` (create), `GET /api/projects/{p}` (read). Map `ProjectExists`→409, `ProjectNotFound`→404 (with `missing_project_msg` on the document/create-feature paths).
- Skills do *not* auto-create projects: on a project-not-found error they surface the explicit-create instruction to the human (projects are rare and deliberate).

### Files

`storage/tracker.py`, `storage/documents.py`, `web/tracker.py`, `web/submit.py`, `web/app.py`.

### Tests

- `POST /api/projects/{p}` creates (200) then 409 on repeat.
- `GET /api/projects/{p}` returns name/repo_path (suggested_order null until P5); 404 if unknown.
- create-feature and document write under an unknown project → clear 404.
- **`feature=None` + missing project**: a project-level document write (e.g. `features.html`, feature segment `-`) requires the project to exist — assert it 404s when the project is absent and succeeds after an explicit create.
- Existing tests that relied on implicit project creation are updated to create the project first — this is broad: most tracker/submit/page tests seed via capture/submit against a fresh DB, so expect to prepend an explicit project (and feature) create across many test setups.

**MR 4.**

## Phase 4

**One write path — fold in the importer (W3); webapp-only.**

### Built

- `walker._process_file` stops duplicating the version/event write logic and delegates the document body to `submit_document`.
- As a bulk loader, the walker declares parents from disk first (`upsert_project`/`upsert_feature`, dependency order), then submits — so a cold-start import into an empty DB bootstraps successfully. A path `identity_for` can't resolve is skipped/warned (orphaned), as today.
- `upsert_project`/`upsert_feature` are now walker-only (the running service uses the strict create + existence checks from P1–3).

### Files

`storage/walker.py` (and its test).

### Tests

- Import a tree into an empty DB → bootstraps it; rows match what API authoring produces (extend the existing convergence test).
- Re-import is idempotent (no spurious versions/events) — the three-state semantics still hold via `submit_document`.
- `reconcile=True` still marks removed docs missing.
- A file at an unresolvable depth is skipped (`identity_for` None), not errored.

**MR 5.**

## Phase 5

**Suggested order in the DB; export reads only from the DB — cross-repo.**

### Built

- Webapp: migration `0007_project_suggested_order.sql` (adds `projects.suggested_order TEXT`); `PUT /api/projects/{p}/suggested-order` (body `{"text": "..."}`); surface `suggested_order` on the single-project GET. Optionally include each feature's `created_at` in the listing.
- Include each feature's `created_at` in the features-listing response (a deliberate inclusion — Nigel floated created-time as about all an agent reader needs alongside the suggested order).
- feature-skills `bin/feature-html-to-md`: replace `_merge_features` / `_merge_features_md` with a render-from-DB path — fetch the features listing + the single-project GET (for `suggested_order`), render the status sections in the fixed section order (In Progress / Available / Parked / Done; `archived` excluded) — the renderer groups by status and imposes that order itself; the listing's `ORDER BY status, slug` is alphabetical and only governs *within-section* row order. Emit `## Suggested order` verbatim between Available and Done. Keep the `--merge-features` flag name (it is called in three skills — `feature-context`, `feature`, `feature-requirements` — so renaming would churn callers) but add a docstring/comment that it now renders from the DB. Render must be deterministic — no dict/set ordering (TESTING.md).

### Files

`storage/migrations/0007_*.sql`, `storage/tracker.py`, `web/tracker.py`, `web/app.py`; feature-skills `bin/feature-html-to-md` + `tests/test_merge_features_md.py`.

### Tests

- Webapp: set then GET suggested_order round-trips; migration applies on a fresh DB.
- Export (rewritten test): a second render of unchanged DB state is byte-identical to the first (renderer idempotence); a notes value set only in the DB appears in the output; the Suggested-order text renders as the `## Suggested order` section between Available and Done; `archived` features are excluded.

**MR 6** (webapp) **+ MR 6b** (feature-skills) — the export change depends on the new single-project GET + `suggested_order`; land webapp first.

## Phase 6

**Listing prepared for search — webapp-only.**

### Built

- `list_features(conn, project_id, *, q: str | None = None, status: str | None = None)` — add `WHERE` clauses: `q` matches slug or notes (`LIKE`), `status` exact-matches; preserve the `status, slug` ordering.
- `list_features_handler` reads `request.query_params.get("q")` / `get("status")` and passes them through.

### Files

`storage/tracker.py`, `web/tracker.py`.

### Tests

- Filter by `q` (matches slug; matches notes; case-insensitive).
- Filter by `status`.
- Both combined; empty result is a valid empty list; no params = unchanged full list.

**MR 7.**

## Phase 7

**Coherent comments / synthesis addressing — documentation (likely no code).**

### Built

- Document doc-id as the canonical write key for comments and synthesis (only the webapp UI writes these today), as a section in `docs/transitions/api-coherence.md` — folding into Phase 0's note rather than a standalone code phase.
- Promote to a real code phase (logical-path write endpoints) only if we later decide agents must write comments/synthesis — not planned now.

### Files

`docs/transitions/api-coherence.md` (addendum).

### Tests

None unless it lands as code; then the chosen addressing is exercised by a test and the documented contract matches behaviour.

**MR 8** (docs) — may be folded into an earlier docs commit.
