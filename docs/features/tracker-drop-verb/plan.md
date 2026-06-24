# tracker-drop-verb — Plan

## Overview

Add a terminal `archived` feature status and a `drop` transition to the tracker, then remove archived features from every surface they could re-appear in. **Phase 1** delivers the core fix: the `archived` status, a `drop_feature` storage transition and its `drop_handler` + route, and a feature-status filter on the two document-driven inbox lanes — after which a dropped feature stops surfacing in Available, the inbox, and the `features.md` export. **Phase 2** adds a read-only, collapsed *Archived* section to the project page so dropped features stay discoverable. Each phase is one MR.

## Key decisions

- **Additive status, no migration.** Add `"archived"` to `FEATURE_STATUSES` in `storage/tracker.py`. Status is enforced in code (the transition functions), not a DB `CHECK` — the `features.status` column is free text, so no schema migration is needed.
- **`drop_feature` mirrors the existing mutation contract** (`claim_feature`/`ship_feature`): 404 if missing, idempotent no-op if already archived, 409 (`InvalidTransition`) on an illegal source status, a history event only on real change. Owner is left untouched (retained as a historical record). Legal transitions: `available→archived`, `in_progress→archived`; `archived→archived` is an idempotent no-op; `done→archived` (and any other source) raises `InvalidTransition`.
  ```
  def drop_feature(conn, *, project, slug, now) -> MutationResult:
      slug = slugify(slug)
      feat = get_feature(conn, project, slug)
      if feat is None:
          raise FeatureNotFound(f"{project}/{slug}")
      if feat["status"] == "archived":
          return MutationResult(project, slug, "archived", changed=False)
      if feat["status"] not in ("available", "in_progress"):
          raise InvalidTransition(f"cannot drop from {feat['status']!r}")
      conn.execute(
          "UPDATE features SET status='archived', updated_at=? WHERE id=?",
          (now, feat["id"]),
      )
      conn.execute(
          "INSERT INTO events (document_id, event_type, payload_json, created_at) "
          "VALUES (NULL, 'feature_dropped', ?, ?)",
          (json.dumps({"project": project, "slug": slug}), now),
      )
      return MutationResult(project, slug, "archived", changed=True)
  ```
- **Inbox: feature-status filter, not doc deactivation.** The `in_progress` lane already filters on feature status. The two document-driven lanes (`new_since_last_visit`, `awaiting_input`) select by *document* status and already JOIN `features f`, so each gets one added clause — `AND f.status != 'archived'`. Documents stay `active` and readable at their `/doc` URLs.
- **Merge-export: no change.** `feature-html-to-md --merge-features` (separate `feature-skills` repo) only places rows whose status is one of available/in_progress/done (`_STATUS_SECTIONS`/`_SECTION_TO_STATUS`), with no fallback re-append, so an archived row is dropped. Unpinned cross-repo coupling — noted, not fixed here.
- **Project page: a fourth read-only bucket.** Add `archived = [f for f in feats if f["status"] == "archived"]` in `project_page.py` and render it as a collapsed `<details>` section in `project.html`, de-emphasised like the existing `done-group`. Read-only: no un-drop action.

## Data model

No schema migration. Changes are confined to application-level values:

- `FEATURE_STATUSES` gains `"archived"` (a fourth terminal status alongside available/in_progress/done).
- A `feature_dropped` event row per drop (`document_id NULL`, `payload_json` = `{"project", "slug"}`), matching the `feature_claimed`/`shipped` precedent.
- The feature's `documents` are untouched — they stay `active`.

## Contract

**New route** (registered in `web/app.py` next to claim/ship):

```
POST /api/projects/{project}/features/{feature}/drop
```

**Request body:** none required. `drop` takes no fields (unlike claim's `owner` / ship's `outcome`). The handler tolerates an empty body; if a body is present it must be a JSON object (otherwise 400) — a minor, deliberate divergence from claim/ship, which always expect a body. Implementation sketch:

```
raw = await request.body()
if raw:
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
# no fields to read; proceed
```

**Responses** (mirroring the claim/ship handlers):

- `200` — `{"project", "slug", "status": "archived", "changed": bool}`. `changed` is `false` for the already-archived no-op. Broadcast fires only when `changed`.
- `404` — feature not found.
- `409` — illegal transition (e.g. dropping a `done` feature).
- `503` — DB not configured.

## File structure

**Phase 1 — drop transition & exclusion**

- `feature_skills_webapp/storage/tracker.py` — M: add `"archived"` to `FEATURE_STATUSES`; add `drop_feature`.
- `feature_skills_webapp/web/tracker.py` — M: add `drop_handler` (import `drop_feature`).
- `feature_skills_webapp/web/app.py` — M: import `drop_handler`, register the `/drop` route (`methods=["POST"]`).
- `feature_skills_webapp/storage/inbox.py` — M: add `AND f.status != 'archived'` to `new_since_last_visit` and `awaiting_input`.
- `feature_skills_webapp/storage/tracker_test.py` — M: drop_feature tests.
- `feature_skills_webapp/web/tracker_test.py` — M: drop_handler tests.
- `feature_skills_webapp/storage/inbox_test.py` — M: archived exclusion tests.

**Phase 2 — Archived section on the project page**

- `feature_skills_webapp/web/project_page.py` — M: add the `archived` bucket; pass to the template.
- `feature_skills_webapp/web/templates/project.html` — M: collapsed *Archived* `<details>` section after Done.
- `feature_skills_webapp/web/project_page_test.py` — M: archived section test.

## Verification

Run from the repo root. All must pass (CI runs the same — see `CLAUDE.md`):

```
uv run ruff format --check .
uv run ruff check .
uv run ty check .
uv run pytest
```

**Phase 1 acceptance** — these targeted runs must select >0 tests and pass (they fail loudly if the transition or filter is absent):

```
uv run pytest feature_skills_webapp/storage/tracker_test.py -k drop -v
uv run pytest feature_skills_webapp/web/tracker_test.py -k drop -v
uv run pytest feature_skills_webapp/storage/inbox_test.py -k archived_feature -v
```

(`-k archived_feature`, not `-k archived`: pre-existing *document*-archival tests already match `archived` and pass regardless of this feature — see the Phase 1 naming note.)

**Phase 1 live check** (Note: requires the running dev service on `127.0.0.1:8800` and a throwaway captured feature):

```
# capture, then drop with an EMPTY body (proves empty-body tolerance), expect status=archived changed=true
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/zz-drop-demo/capture -d '{}'
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/zz-drop-demo/drop   # no -d: empty body
# dropping again is an idempotent no-op (changed=false)
curl -fsS -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/zz-drop-demo/drop -d '{}'
# a non-existent feature returns 404 (use -o/-w, not -f, so the expected 404 doesn't fail the shell)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8800/api/projects/feature-skills-webapp/features/zz-no-such/drop -d '{}'  # -> 404
```

**Phase 2 acceptance**:

```
uv run pytest feature_skills_webapp/web/project_page_test.py -k archived -v
```

**Phase 2 DOM assertion** (Note: requires the running dev service and a dropped feature, e.g. `zz-drop-demo` from above): the project page renders an Archived section containing the dropped slug, and that slug must *not* appear in the active groups.

```
curl -fsS http://127.0.0.1:8800/project/feature-skills-webapp | grep -i 'Archived'   # section present
curl -fsS http://127.0.0.1:8800/project/feature-skills-webapp | grep 'zz-drop-demo'  # listed (under Archived)
```

## Qc

Follow whatever `CLAUDE.md` specifies at implementation time. As of now that is the four commands in Verification (ruff format, ruff check, ty check, pytest), all green, before committing each phase's MR. If you exercise the live service rather than the test suite, restart it per `CLAUDE.md` (`systemctl --user restart feature-skills-webapp`) — code edits don't reach the running `uv tool`-installed service until restart.

## Checklist

### Phase 1: Drop transition & exclusion

- Add `"archived"` to `FEATURE_STATUSES` in `storage/tracker.py`.
- Add `drop_feature` to `storage/tracker.py` (transition matrix; `feature_dropped` event; owner retained).
- Add `drop_handler` to `web/tracker.py` (empty-body-tolerant; 404/409 mapping; broadcast on change).
- Register the `POST .../drop` route in `web/app.py`.
- Add `AND f.status != 'archived'` to `new_since_last_visit` and `awaiting_input` in `storage/inbox.py`.
- Write `drop_feature` storage tests (all transition-matrix edges + event/no-event assertions).
- Write `drop_handler` tests (200/404/409/400/503, broadcast on change vs no-op).
- Write inbox exclusion tests: archived feature absent from both document-driven lanes.
- Run the four QC commands; all green. Commit + open Phase 1 MR.

### Phase 2: Archived section on the project page

- Add the `archived` bucket in `web/project_page.py` and pass it to the template.
- Render a collapsed, read-only Archived section in `web/templates/project.html`.
- Write the project-page test: dropped feature appears under Archived and nowhere else.
- Run the four QC commands; all green. Commit + open Phase 2 MR.

## Phase 1

**Goal:** a dropped feature stops surfacing in Available, the inbox, and the `features.md` export.

**Build:**

- `storage/tracker.py`: `FEATURE_STATUSES = ("available", "in_progress", "done", "archived")`; add `drop_feature` per the Key-decisions snippet.
- `web/tracker.py`: `drop_handler` following the `ship_handler` shape — the empty-body-tolerant parse from the Contract section, then `with request_conn(...) as conn, transaction(conn): drop_feature(...)`, mapping `FeatureNotFound`→404 and `InvalidTransition`→409, broadcasting only when `result.changed`.
- `web/app.py`: register `Route(".../drop", drop_handler, methods=["POST"])`.
- `storage/inbox.py`: add `AND f.status != 'archived'` to `new_since_last_visit` and `awaiting_input`. **Placement matters:** both queries build a base WHERE string, then *conditionally* append `AND d.project_id = ?` and finally `ORDER BY …`. Add the new clause to the **base WHERE string** (before the project_id / ORDER BY concatenation) — appending it after `ORDER BY` is invalid SQL and would break the inbox for *all* features. Leave `in_progress` and `recently_shipped` untouched (the former already filters on feature status; the latter reads `shipped` events and never consults current status).

**Tests** (mirror existing claim/ship test style — seed helpers `_seed_feature` / `_seed_bare_feature`, assert observable state):

- storage: `available→archived` sets status + emits a `feature_dropped` event + `changed=True`; `in_progress→archived` allowed and **owner retained**; `done→archived` raises `InvalidTransition` with no event and status unchanged; `archived→archived` no-op (no event, `changed=False`); missing feature raises `FeatureNotFound`.
- handler: 200 + `status=archived`/`changed=True`; already-archived 200/`changed=False`; 404 missing; 409 from done; 400 on a non-object body; 503 when no DB; broadcasts on change, not on no-op.
- inbox: seed an `archived` feature with an `active` doc that would otherwise surface — an unread event for `new_since_last_visit`, and a `*-feedback` doc with no synthesis response for `awaiting_input` — and assert the feature is **absent** from both lanes. This is the regression most likely to be reintroduced, so pin it explicitly. **Naming:** `inbox_test.py` already has tests with `archived` in the name that exercise *document*-archival (e.g. `test_new_since_excludes_archived_missing_read`, `test_awaiting_input_archived_doc_never_appears`) and pass regardless of the new filter. Name these new *feature*-archival tests with a distinct token — `..._archived_feature_...` — so the Verification selector can target them. Per TESTING.md rule 1, confirm each new test goes **red** with the `f.status != 'archived'` clause removed before trusting it.

## Phase 2

**Goal:** dropped features remain discoverable via a read-only collapsed Archived section on the project page.

**Build:**

- `web/project_page.py`: add `archived = [f for f in feats if f["status"] == "archived"]` and pass `"archived": [_feat(f) for f in archived]` in the template context.
- `web/templates/project.html`: after the Done group, add a collapsed section, de-emphasised like `done-group`: (Exact markup is the implementer's call; keep it read-only — no status buttons — and collapsed by default.) Update the empty-state guard (`{% if not in_progress and not available and not done %}`) only if an archived-only project should still show the list rather than “No features yet” — minor, implementer's discretion.
  ```
  {% if archived %}
  <details class="feat-group done-group">
    <summary><h2 style="display:inline">Archived</h2></summary>
    <ul class="feat-list">
      {% for feat in archived %}
      <li><a class="feat-row" href="/project/{{ project | urlencode }}/feature/{{ feat.slug | urlencode }}"
             aria-label="{{ feat.slug }}">
        <span class="feat-slug">{{ feat.slug }}</span>
        {% if feat.owner %}<span class="feat-owner">{{ feat.owner }}</span>{% endif %}
      </a></li>
      {% endfor %}
    </ul>
  </details>
  {% endif %}
  ```

**Test** (extend `project_page_test.py`): set a feature to `archived` (capture then drop via the API, as the existing grouping test does for claim/ship), GET `/project/<proj>`, assert the response contains an *Archived* section with the slug and that the slug is absent from the In progress / Available / Done groups.
