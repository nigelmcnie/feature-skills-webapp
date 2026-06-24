# tracker-lifecycle-transitions — Plan

## Overview

Complete the tracker's non-terminal lifecycle with three new moves, all following the existing mutation contract in `storage/tracker.py`: a `release` transition (in_progress→available), a `parked` status with a `park` transition (available|in_progress→parked) and resume-by-claim, and a backfill path (available→done by widening `ship`). Parked work is surfaced everywhere it could be read — project page, inbox, and the `features.md` exporter — so deferring a feature never makes it vanish. Four MRs (P1 release, P2 parked, P3 backfill in this repo; P4 exporter in the feature-skills repo) plus a one-off dog-food closeout.

## Key decisions

- **Mirror the existing transition functions.** `release_feature` and `park_feature` are written exactly like `claim_feature`/`ship_feature`: load via `get_feature`, return `MutationResult(..., changed=False)` on a redundant move (no event), raise `InvalidTransition` on a bad source (→409), raise `FeatureNotFound` when absent, otherwise `UPDATE` + insert an `events` row and return `changed=True`.
- **Widen, don't fork, claim and ship.** Resume and backfill are not new verbs — they widen the accepted source-state set of existing functions:
  ```
  # claim: available OR parked -> in_progress
  if feat["status"] == "in_progress": return MutationResult(..., changed=False)
  if feat["status"] not in ("available", "parked"): raise InvalidTransition(...)

  # ship: in_progress OR available -> done   (parked rejected)
  if feat["status"] == "done": return MutationResult(..., changed=False)
  if feat["status"] not in ("in_progress", "available"): raise InvalidTransition(...)
  ```
- **Owner is cleared but preserved in the event.** `park`/`release` set `owner=NULL` on the row, and their event payload is `{project, slug, owner}` where `owner` is the value being cleared (read from the feature row before the UPDATE).
- **Resume reuses `feature_claimed`.** No `feature_resumed` event type.
- **Read-site safety is by allow-list.** Existing buckets (`project_page` available/done, `inbox.in_progress`, `recently_shipped`) test for an explicit status/event, so `parked` is excluded automatically. The work is *adding* a Parked bucket/category, not adding exclusions.
- **park/release take no request body.** Unlike `claim` (needs owner) and `ship` (optional outcome), the new handlers require no input. Do *not* copy claim/ship's `await request.json()` + non-dict→400 block, or an empty `POST` (no `-d`) would 400 — the handlers tolerate an absent/empty body.
- **Exporter must create the section.** `feature-html-to-md`'s merge is block-driven — it only emits status sections that already exist as `##` blocks in `features.md`. So Phase 4 must both teach it the `"Parked"→"parked"` mapping *and* synthesise a `## Parked` section when parked features exist and no block is present (else parked features are still dropped).

## Data model

- `FEATURE_STATUSES` in `storage/tracker.py` becomes `("available", "in_progress", "parked", "done")`. No DB `CHECK`, no migration.
- No new columns. `features.owner` (existing, nullable) is the only column touched, set to `NULL` by park/release.
- Two new `events.event_type` values: `feature_parked` and `feature_released`, each `document_id=NULL` with payload `{"project", "slug", "owner"}`. Resume reuses `feature_claimed`; backfill reuses `shipped` (payload `{project, slug}`, so the inbox Recently-shipped query keeps working unchanged).
- Caveat: one hand-made `feature_released` row already exists in the live DB — harmless (free-text type); tests use per-worker fresh DBs.

## Contract

**Transition matrix** (target ← legal sources; everything else → 409):

- `park` → `parked` from `{available, in_progress}`; clears owner. `parked` source → no-op (changed=False). `done` → 409.
- `release` → `available` from `{in_progress}`; clears owner. `available` source → no-op. `done`/`parked` → 409.
- `claim` → `in_progress` from `{available, parked}`; sets owner. `in_progress` source → no-op. `done` → 409.
- `ship` → `done` from `{in_progress, available}`. `done` source → no-op. `parked` → 409.

**HTTP routes** (POST, mirroring claim/ship handler error mapping — FeatureNotFound→404, InvalidTransition→409, broadcast only when `result.changed`):

- `POST /api/projects/{project}/features/{feature}/park` → `park_handler` (no body).
- `POST /api/projects/{project}/features/{feature}/release` → `release_handler` (no body).

Response shape is identical to claim/ship: `{project, slug, status, changed}`.

## File structure

**Modified (webapp repo, Phases 1–3):**

- `feature_skills_webapp/storage/tracker.py` — `FEATURE_STATUSES`; new `release_feature`, `park_feature`; widen `claim_feature` + `ship_feature` source guards.
- `feature_skills_webapp/web/tracker.py` — new `park_handler`, `release_handler`.
- `feature_skills_webapp/web/app.py` — register the two new routes; import the handlers.
- `feature_skills_webapp/storage/inbox.py` — new `parked()` query; `Inbox.parked` field; update `is_empty`, `build_inbox`, and the early `Inbox([], …)` return.
- `feature_skills_webapp/web/project_page.py` — `parked` list + template context.
- `feature_skills_webapp/web/templates/_inbox_body.html` — Parked category section.
- `feature_skills_webapp/web/templates/index.html` — `.badge-parked` CSS.
- `feature_skills_webapp/web/templates/project.html` — Parked group + no-features guard.

**Test files (mirroring existing *_test.py):** `storage/tracker_test.py`, `web/tracker_test.py`, `storage/inbox_test.py`, `web/project_page_test.py`, `web/feature_page_test.py`.

**Modified (feature-skills repo, Phase 4):** `~/src/nigelmcnie/feature-skills/bin/feature-html-to-md` + its test file.

**Closeout:** `features.md` (webapp repo) gains a `## Parked` row; `review-severity-recalibration`'s context/requirements doc bodies lose the `PARKED` banner.

## Verification

Run from the webapp repo root unless noted. Each command fails loudly if the behaviour is absent. **Before any live-`curl` check, restart the service** (`systemctl --user restart feature-skills-webapp`; reinstall first if deps changed — see `CLAUDE.md`) or you'll be testing the old code.

**Phase 1:**

```
uv run pytest -k release
# live API (service running on :8800), against a throwaway feature:
curl -fsS -X POST http://127.0.0.1:8800/api/projects/<P>/features/<F>/release  # 200 {"status":"available","changed":true}
curl -fsS -X POST http://127.0.0.1:8800/api/projects/<P>/features/<F>/release  # 200 changed:false (idempotent)
```

**Phase 2:**

```
uv run pytest -k "park or parked"
uv run pytest feature_skills_webapp/web/project_page_test.py feature_skills_webapp/storage/inbox_test.py feature_skills_webapp/web/feature_page_test.py
# project page JSON shows the parked feature in its own bucket, not available:
curl -fsS http://127.0.0.1:8800/api/projects/<P>/features | python3 -c "import sys,json;print([f for f in json.load(sys.stdin)['features'] if f['status']=='parked'])"
```

**Phase 3:**

```
uv run pytest -k "backfill or ship"
# ship from available → done; ship from parked → 409
curl -fsS -X POST http://127.0.0.1:8800/api/projects/<P>/features/<avail-F>/ship  # 200 status:done
```

**Phase 4 (feature-skills repo):** run that repo's exporter test suite (e.g. `uv run pytest` / its documented runner) — a parked feature appears under `## Parked` in the merged output, not `## Available`.

**Whole-suite gate (every phase):** `uv run pytest` green (full xdist suite, not a subset).

## Qc

Per `CLAUDE.md`, run all of these before committing each phase; all must pass:

```
uv run ruff format --check .
uv run ruff check .
uv run ty check .
uv run pytest
```

The implementing agent follows whatever `CLAUDE.md` says at implementation time. For the feature-skills repo (Phase 4), follow that repo's own QC conventions.

## Checklist

### Phase 1: Release transition

- Add `release_feature` to `storage/tracker.py` (in_progress→available, clear owner, `feature_released` event with cleared owner; available→no-op; done/parked→InvalidTransition).
- Add `release_handler` to `web/tracker.py` and register `POST .../release` in `app.py`.
- Storage tests: release from in_progress (status+owner+event payload), no-op from available, 409 from done & parked, FeatureNotFound when missing.
- Route tests: 200 changed=true, repeat changed=false, 409 on done, broadcast only on real change.
- Run QC (ruff format/check, ty, full pytest); commit; raise MR.

### Phase 2: Parked status + park + resume + read sites

- Add `parked` to `FEATURE_STATUSES`.
- Add `park_feature` (available|in_progress→parked, clear owner, `feature_parked` event with cleared owner; parked→no-op; done→409).
- Widen `claim_feature` source to `{available, parked}` (resume reuses `feature_claimed`).
- Add `park_handler` and register `POST .../park` in `app.py`.
- inbox.py: add `parked()` query, `Inbox.parked` field, update `is_empty`, `build_inbox`, and the early `Inbox([…])` return.
- Templates: Parked category in `_inbox_body.html`; `.badge-parked` CSS in `index.html`.
- project_page.py: parked list + context; `project.html` Parked group + extend no-features guard.
- Storage tests: park from available & in_progress, no-op from parked, 409 from done, claim-from-parked resume.
- Read-site tests: project_page (parked bucket, absent from available/in_progress), inbox (parked category, absent from in_progress/shipped, is_empty false), feature_page renders parked.
- Run QC; commit; raise MR.

### Phase 3: Backfill available→done

- Widen `ship_feature` source to `{in_progress, available}` (parked stays 409).
- Replace `test_ship_available_feature_raises_invalid_transition` with a backfill test (available→done, `shipped` event); add ship-from-parked→409.
- Route test: backfill 200 changed=true; parked→409. Confirm inbox recently_shipped sees a backfilled feature.
- Run QC; commit; raise MR.

### Phase 4: Exporter ## Parked section (feature-skills repo)

- Add `"Parked"` to `_STATUS_SECTIONS` and `"Parked":"parked"` to `_SECTION_TO_STATUS`.
- Parked table header (Feature | Notes) + `_format_row` Parked branch (like Available).
- Synthesise a `## Parked` section inline after the In Progress block (fallback: end of doc) when parked features exist and no block present; byte-compatible with the normal-path rebuild so re-merge is idempotent.
- Tests: parked emitted under ## Parked not ## Available; moved-from-other-section; idempotency asserts merge-of-merge equals merge byte-for-byte. Run the feature-skills repo QC; raise MR there.

### Closeout: dog-food correction

- Park `review-severity-recalibration` via the new endpoint, confirm it exports under ## Parked, and remove its `PARKED` doc-body banner.

## Phase 1

### Phase 1 — Release transition (1 MR)

**Build.** `release_feature(conn, *, project, slug, now) -> MutationResult` in `storage/tracker.py`:

```
feat = get_feature(conn, project, slug)
if feat is None: raise FeatureNotFound(f"{project}/{slug}")
if feat["status"] == "available": return MutationResult(project, slug, "available", changed=False)
if feat["status"] != "in_progress": raise InvalidTransition(f"cannot release from {feat['status']!r}")
owner = feat["owner"]
conn.execute("UPDATE features SET status='available', owner=NULL, updated_at=? WHERE id=?", (now, feat["id"]))
conn.execute("INSERT INTO events (document_id, event_type, payload_json, created_at) "
             "VALUES (NULL, 'feature_released', ?, ?)",
             (json.dumps({"project": project, "slug": slug, "owner": owner}), now))
return MutationResult(project, slug, "available", changed=True)
```

`release_handler` in `web/tracker.py` mirrors `ship_handler` minus the body parsing (no outcome/owner); map FeatureNotFound→404, InvalidTransition→409, broadcast when changed. Register `POST .../release` in `app.py`.

**Tests.** storage: release from in_progress (status=available, owner is NULL, exactly one `feature_released` event whose payload `owner` is the cleared name); no-op from available (changed=False, no event); 409 from done and from parked; FeatureNotFound when missing. route: 200 changed=true; repeat 200 changed=false; 409 on done; broadcast called once (on the real change only).

No read-site changes (no new status). MR title: `feat(tracker): release transition (in_progress→available)`.

## Phase 2

### Phase 2 — Parked status + park + resume + read sites (1 MR)

**Storage.** Add `parked` to `FEATURE_STATUSES`. Add `park_feature(conn, *, project, slug, now)`: no-op if already parked; 409 if `done`; else from `{available, in_progress}` set `status='parked', owner=NULL` and emit `feature_parked` with `{project, slug, owner}` (owner read before the UPDATE). Widen `claim_feature`: replace `if feat["status"] != "available"` with `if feat["status"] not in ("available", "parked")` — a parked feature claimed sets the new owner, lands in_progress, emits the existing `feature_claimed`.

**Route.** `park_handler` (no body) + `POST .../park` in `app.py`.

**Read sites.**

- `inbox.py`: add `parked(conn, project_id=None)` modelled on `in_progress` (`WHERE f.status = 'parked'`, `_feature_card(r, label="Parked", badge="parked")`); add `parked: list[InboxCard]` to the `Inbox` dataclass; include it in `is_empty`, in `build_inbox`, and in the early `Inbox([], [], [], [])` return (now five lists).
- `_inbox_body.html`: a Parked `<section class="category">` after "In progress", copying the in_progress card markup (feature links to the feature page). `index.html`: add `.badge-parked { color: var(--accent); background: var(--accent-soft); }` beside `.badge-in-progress`.
- `project_page.py`: `parked = [f for f in feats if f["status"] == "parked"]`; pass `"parked": [_feat(f) for f in parked]`. `project.html`: a Parked `feat-group` after Available; extend the no-features guard to `and not parked`.
- `feature_page.py`: no code change (status is passed through to `feature.html`); covered by a render test.

**Tests.** storage: park from available and from in_progress (owner cleared, payload owner correct); no-op from parked; 409 from done; claim-from-parked resumes (owner set, status in_progress, `feature_claimed` emitted, no `feature_resumed`). read sites: `project_page` JSON has the parked feature under `parked` and absent from `available`/`in_progress`; `inbox` parked feature present in `parked`, absent from `in_progress`/`recently_shipped`; `is_empty` false when only a parked feature exists; `feature_page` returns 200 and shows the parked status for a parked feature. MR title: `feat(tracker): parked status + park/resume + read sites`.

## Phase 3

### Phase 3 — Backfill available→done (1 MR)

**Build.** In `ship_feature`, replace `if feat["status"] != "in_progress"` with `if feat["status"] not in ("in_progress", "available")`. `parked` falls through to the `InvalidTransition` (→409). The done-no-op and optional-outcome behaviour are unchanged; a backfilled feature still emits the existing `shipped` event so it appears in the inbox Recently-shipped category.

**Tests.** *Replace* the existing `test_ship_available_feature_raises_invalid_transition` (ship-from-available is now legal) with a backfill test: ship from available → status=done, a `shipped` event emitted. Add: ship from parked → 409. Route test: backfill returns 200 changed=true; parked → 409. Confirm an inbox `recently_shipped` test sees a backfilled feature. MR title: `feat(tracker): backfill available→done via ship`.

**Ordering.** Although each phase is a clean standalone MR, the ship-from-parked→409 test here depends on Phase 2 having introduced `parked` — so land this after Phase 2 (the 1→2→3 order already satisfies it; the phases are not freely reorderable).

## Phase 4

### Phase 4 — Exporter `## Parked` section (feature-skills repo, 1 MR)

In `~/src/nigelmcnie/feature-skills/bin/feature-html-to-md`:

- Add `"Parked"` to `_STATUS_SECTIONS` and `"Parked": "parked"` to `_SECTION_TO_STATUS`.
- Parked uses the same columns as Available (owner is cleared): in `_merge_features_md` the Parked `tbl_header` is `["| Feature | Notes |\n", "|---|---|\n"]`, and `_format_row` gets a Parked branch identical to its Available branch.
- **Order of work matters.** Add the `_format_row` Parked branch (above) *before* the synthesis step, since synthesis renders rows by calling `_format_row(h="Parked", lc, db_feat, None)` — the same path step-3 uses — for determinism.
- **Section creation — pin the mechanic.** The merge appends each block to `result` as it iterates (it's not a post-loop splice), so emit the synthesised block *inline*: when handling the `## In Progress` block, after appending it, check whether any DB feature is `parked` and no `## Parked` block exists among the file's headings; if so, append the synthesised `## Parked` heading + `| Feature | Notes |` header + parked rows right then (falling back to end-of-document if there is no In Progress block). Track "Parked block already present" from the initial heading scan so the synthesis fires at most once.
- **Idempotency hinges on byte-compatibility.** Once a `## Parked` block exists, the normal path (`_split_around_table` + rebuild) regenerates it — but only if the synthesised block is byte-identical to what the normal path would produce (same header, rows via `_format_row`). So the synthesised block must match the regenerated form exactly.

**Tests** (match the repo's existing exporter tests): merge with a parked DB feature emits it under `## Parked` and not under `## Available`; a parked feature that previously had an In Progress / Available row is moved to Parked; **idempotency: merge-of-merge equals merge byte-for-byte** (not just "no duplicate section"). Run the feature-skills repo's own QC. MR is raised in that repo.

## Phase 5

### Closeout — dog-food correction (no MR / operational)

Once Phases 1–4 are live: park `review-severity-recalibration` via `POST /api/projects/feature-skills-webapp/features/review-severity-recalibration/park`, run the `features.md` merge-export to confirm it lands under `## Parked`, and remove the `PARKED` banner from its context/requirements doc bodies (via the documents API) so tracker status is the single source of truth.
