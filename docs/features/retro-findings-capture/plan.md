# Retro findings capture

## Overview

Persist `/feature-retro`'s discussion-class findings in the webapp so the next retro can read what earlier ones flagged and detect recurrence across features. Two new tables (`retro_runs`, `retro_findings`) and a small HTTP surface modelled on `web/synthesis.py`. **Phase 1** delivers the store and the capture/query endpoints (closing the loop, no UI). **Phase 2** surfaces findings for review and lets the developer set their triage status. The recurrence-trend signal is out of scope (split to the `retro-recurrence-trend` feature).

## Key technical decisions

1. **Two tables: a run owns its findings**
  A `retro_runs` row is one `/feature-retro` invocation for a project; `retro_findings` rows belong to a run. The run carries an agent-supplied `run_key` that is the idempotency handle (decision 2). Findings are denormalised with `project_id` and the `feature` tag for direct querying without always joining through the run.
2. **Idempotent capture: replace-by-run, mirroring synthesis**
  `synthesis.py` gets idempotency by deleting a document's responses then re-inserting. We do the same keyed on the run: a `UNIQUE(project_id, run_key)` constraint, and on POST we delete any existing run with that key (`ON DELETE CASCADE` removes its findings) and recreate it. Re-running a retro therefore replaces rather than duplicates — the "graveyard" failure mode can't be reached by the write path. **This makes `run_key` load-bearing**: see the producer contract in the HTTP section.
3. **Status and recurrence are separate axes**
  Per requirements round 1: `status` is a developer-driven lifecycle column (`open | actioned | deferred | rejected`); recurrence is an agent-observed property expressed by a self-referential `recurs_from` FK pointing at the canonical original (a star, not a chain). "Recurring" is never a status — recurrence depth is a `COUNT` of children.
  ```text
  status:  open ──▶ actioned   (developer)
                ├▶ deferred
                └▶ rejected

  recurs:  finding#42 ─recurs_from─▶ finding#14 (original)
           finding#58 ─recurs_from─▶ finding#14
           → recurrence_count(#14) = 2
  ```
4. **Project resolved by name; feature is a run-level free-text tag**
  The agent sends the project *name* (the `basename` it already derives); the handler resolves it to `project_id` or returns 404 — exactly how `synthesis.py` treats a missing document. The originating `feature` is supplied **once, on the run** (not per finding) and stored as free text, not a FK, so a retro can capture findings for a feature the webapp never ingested. Each `retro_findings` row copies `feature` from its run at insert time for query convenience; the client never sets it per-finding.
5. **Top-level endpoints, synthesis conventions**
  There is no document to scope to, so the routes are top-level (`/retro-findings`) with the project in the body / query string, rather than `/doc/{id}/…`. Everything else follows `synthesis.py`: `request_conn`, validate fully (types + 1 MB cap) *before* `transaction()` / `BEGIN IMMEDIATE`, `broadcaster.broadcast()` after a successful write, 503 when `db_path is None`. Validations needing a DB read (project resolution; `recurs_from` existence) run inside the `with request_conn` block but still before `transaction()` — there is no way to validate them with no connection open.
6. **Recurrence link: self-run guard + graceful loss**
  `recurs_from` is `ON DELETE SET NULL`. Two edges handled deliberately:
    - **Self-run reference.** A re-posted run deletes its own findings inside the transaction. So a finding may only cite a `recurs_from` that belongs to a *different* run; a reference to a finding in the run being posted/replaced is rejected with **400** (not allowed to silently dangle or 500 on the FK).
    - **Original re-posted later.** If the original finding's run is itself re-posted (recreating that finding with a new id), children lose the link (set to NULL) rather than dangling; the next retro re-establishes recurrence when it reads the priors. Acceptable at this volume — consistent with agent-judged recurrence.
7. **Status changes audited via the existing events table**
  A status change appends an `events` row with `document_id = NULL` (the column is nullable, `ON DELETE SET NULL`), `event_type = 'retro_finding_status_changed'`, and `payload_json` carrying `{finding_id, old_status, new_status}` — the pattern `comments.py` already uses. A **no-op** status change (the finding already has that status) returns 200 but writes *no* event, so the audit log records only real transitions.
8. **Phase 2 surfaces on the project page, not the document inbox**
  Findings are project-scoped and are not `documents`, so they don't fit the document-centric inbox queries (`build_inbox` joins `documents`). The project page (`project_page.py` / `project.html`) is already project-scoped and is the natural home — a "Process findings" panel. This resolves the requirements' open surfacing question; revisit at Phase 2 implementation if a cross-inbox badge proves worth it.

## Data model & migration

New migration `storage/migrations/0004_retro_findings.sql`. Follow the existing convention: plain DDL split naively on `;` (no triggers or semicolons-in-strings), ending with a `schema_version` bump to 4. Match the trailing-semicolon style of the neighbouring migrations (0001/0003 omit the final `;`; either is tolerated by the splitter — just be consistent).

```sql
CREATE TABLE retro_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_key     TEXT NOT NULL,          -- agent-supplied; idempotency handle
    feature     TEXT,                   -- originating feature slug (free-text tag)
    ran_at      TEXT,                   -- agent-supplied retro time (optional)
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, run_key)
);

CREATE TABLE retro_findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES retro_runs(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    feature     TEXT,                   -- copied from the run at insert
    title       TEXT NOT NULL,
    evidence    TEXT,
    change      TEXT,                   -- proposed change / question to discuss
    status      TEXT NOT NULL DEFAULT 'open',  -- open|actioned|deferred|rejected
    recurs_from INTEGER REFERENCES retro_findings(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_retro_runs_project        ON retro_runs(project_id);
CREATE INDEX idx_retro_findings_project    ON retro_findings(project_id);
CREATE INDEX idx_retro_findings_run        ON retro_findings(run_id);
CREATE INDEX idx_retro_findings_recurs_from ON retro_findings(recurs_from);

INSERT INTO schema_version (version) VALUES (4);
```

All timestamps come from `db.now_iso()` (the single source of truth — `created_at` is compared lexicographically for "long-open" ordering). `status` is intentionally **not** indexed: the GET filters on it, but the per-project finding count is far too low for the index to matter. `ran_at` is stored but read by nothing in this feature — it's there for the future `retro-recurrence-trend` work.

## HTTP contract

The joint contract between `feature-skills` (writer/reader) and this repo (store). Exact field names below are the contract; the `feature-skills` SKILL.md change is tracked separately in that repo.

### Producer obligation: the run key

Idempotency hinges entirely on `run.key` (decision 2). The producer (`/feature-retro`) must emit a key that is **stable across re-posts of the same retro** (so a correction replaces rather than duplicates) yet **unique across distinct retros** (so two real retros never collide). A value like `<feature>-<ISO-timestamp-of-the-retro>` satisfies both. The webapp treats the key as opaque; honouring this property is the producer's responsibility.

### POST /retro-findings — capture a run (Phase 1)

```json
{
  "project": "feature-skills-webapp",
  "run":     { "key": "doc-view-2026-06-14T22:01Z", "feature": "doc-view",
               "ran_at": "2026-06-14T22:01:00Z" },
  "findings": [
    { "title": "Plan review re-asks phase ordering every time",
      "evidence": "...", "change": "...", "recurs_from": 14 }
  ]
}
→ 200 { "run_id": 7, "findings_written": 1 }
→ 404 project not found · 400 malformed / bad recurs_from · 503 db not configured
```

### GET /retro-findings?project=<name> — query priors (Phase 1)

```json
→ 200 {
  "project": "feature-skills-webapp",
  "findings": [
    { "id": 14, "title": "...", "evidence": "...", "change": "...",
      "status": "open", "feature": "doc-view", "recurs_from": null,
      "recurrence_count": 2, "created_at": "..." }
  ]
}
```

Returns findings whose status is `open` or `deferred` (i.e. not `actioned` / `rejected`) — the "still relevant" set. Each row carries its `id`, so the next POST can cite it as `recurs_from`; that read-returns-ids → write-cites-id round trip is the core mechanic. Recurrences are returned as **their own rows** (not folded into the parent), so the producer-side skill walks `recurs_from` to assemble the in-session "this echoes #14" view. `recurrence_count` counts a finding's children **of any status** (the relationship is what matters, not the children's triage state).

### POST /retro-findings/{id}/status — triage (Phase 2)

```json
{ "status": "actioned" }   // one of: open|actioned|deferred|rejected
→ 200 { "id": 14, "status": "actioned" }   // no-op if unchanged; no event written
→ 404 finding not found · 400 invalid status · 503 db not configured
```

## File structure

### New files

- `feature_skills_webapp/storage/migrations/0004_retro_findings.sql` — schema (Phase 1).
- `feature_skills_webapp/web/retro_findings.py` — endpoint handlers (Phase 1; status handler added Phase 2).
- `feature_skills_webapp/web/retro_findings_test.py` — endpoint tests (Phase 1 & 2).
- `feature_skills_webapp/storage/retro_findings.py` — read model for surfacing (Phase 2).
- `feature_skills_webapp/storage/retro_findings_test.py` — read-model tests (Phase 2).

### Modified files

- `feature_skills_webapp/web/app.py` — register routes (both phases).
- `feature_skills_webapp/storage/db_test.py` — assert migrate() reaches v4 + tables exist (confirm this file exists; if not, add the assertion wherever migration tests live).
- `feature_skills_webapp/web/project_page.py` — load findings for the page (Phase 2).
- `feature_skills_webapp/web/templates/project.html` — "Process findings" panel + status JS (Phase 2).
- `feature_skills_webapp/web/project_page_test.py` — panel assertions (Phase 2).

## Phase 1 — Findings store + capture/query contract

### What's built

The migration, plus `web/retro_findings.py` with the two Phase-1 handlers, registered in `app.py`. After this phase the next `/feature-retro` can POST a run and GET the project's priors.

### Handlers (signatures & behaviour)

```python
_MAX_VALUE_BYTES = 1024 * 1024  # 1 MB, per text field (matches synthesis)
_ALLOWED_STATUS = {"open", "actioned", "deferred", "rejected"}

async def post_retro_findings(request: Request) -> JSONResponse:
    # 503 if app.state.db_path is None
    # parse JSON → 400 if not a dict
    # project: str (required) ; run: dict with key:str (required, non-empty),
    #   feature:str|None, ran_at:str|None ; findings: list (required)
    # each finding: title:str (required, non-empty), evidence/change optional
    #   str, recurs_from optional int. (feature is run-level, NOT per finding.)
    #   Validate types + 1 MB cap on each text field → 400 BEFORE opening conn.
    # with request_conn:                     # DB reads, still pre-transaction
    #   resolve project by name → 404 if missing
    #   for each recurs_from: must reference an existing retro_findings row in
    #     THIS project that is NOT in the run being replaced (project_id match +
    #     run_key != this run_key) → 400 otherwise
    #   transaction():
    #     DELETE FROM retro_runs WHERE project_id=? AND run_key=?   # cascade
    #     INSERT retro_runs(project_id, run_key, feature, ran_at, created_at=now)
    #     INSERT each retro_findings(run_id, project_id, feature(=run.feature),
    #       title, evidence, change, status='open', recurs_from,
    #       created_at=now, updated_at=now)
    # broadcast(); return {"run_id": run_id, "findings_written": n}

async def get_retro_findings(request: Request) -> JSONResponse:
    # 503 if db_path is None ; project query param required → 400 if absent
    # with request_conn: resolve project by name → 404 if missing
    #   SELECT findings WHERE project_id=? AND status IN ('open','deferred')
    #     with recurrence_count =
    #       (SELECT COUNT(*) FROM retro_findings c WHERE c.recurs_from = f.id)
    #     ORDER BY created_at, id
    # return {"project": name, "findings": [ {id,title,evidence,change,status,
    #   feature,recurs_from,recurrence_count,created_at}, ... ]}
```

Register in `app.py` routes list (two `Route`s on the same path with different methods, as `/comments` does):

```python
Route("/retro-findings", post_retro_findings, methods=["POST"]),
Route("/retro-findings", get_retro_findings),  # GET
```

### Tests

- Write-then-read round trip: POST a run with two findings, GET returns both with ids, statuses `open`, `feature` copied from the run.
- Idempotent re-post: POST run key K with 3 findings, POST K again with 1 — DB holds only the second set (mirrors `test_repost_replaces_item_set`).
- Recurrence round trip: POST run A (finding gets id X); GET to learn X; POST run B with `recurs_from: X`; GET shows `recurrence_count == 1` on finding X.
- Self-run `recurs_from` rejected: POST a run whose finding cites a `recurs_from` belonging to that same run key → 400 (not 500).
- Cross-project `recurs_from` rejected: `recurs_from` an id that exists but in another project → 400.
- Read filter: a finding seeded with status `actioned`/`rejected` (direct DB insert in Phase 1) is excluded; `deferred` is included.
- 404 unknown project (POST and GET); 400 missing project / missing run.key / empty findings-element title / non-list findings / oversize field; 503 when db not configured.
- Broadcast fires on POST (register a queue, assert non-empty — as `test_post_broadcasts`).

### MR chain

One MR titled `feat(retro-findings-capture): phase 1`.

## Phase 2 — Surface & triage

### What's built

A read model, the status-change endpoint, and a "Process findings" panel on the project page where recurring / long-open findings are prominent and the developer can set status.

### Read model — `storage/retro_findings.py`

```python
@dataclass(frozen=True)
class FindingRow:
    id: int
    title: str
    evidence: str | None
    change: str | None
    status: str
    feature: str | None
    recurs_from: int | None
    recurrence_count: int
    created_at: str

def list_findings(conn, project_id: int) -> list[FindingRow]:
    # all findings for the project (any status, so the panel can show actioned
    #   ones too if it wants tabs); recurrence_count via COUNT subquery.
    # ORDER BY: recurring first (recurrence_count DESC), then oldest-open first
    #   (created_at ASC) so long-open items rise. Tie-break on id for determinism.
```

The GET endpoint's inline query from Phase 1 may be refactored to share this helper (filtering to open/deferred) so contract and page have one source of truth — optional; note it but don't force it.

### Status endpoint — add to `web/retro_findings.py`

```python
async def post_retro_finding_status(request: Request) -> JSONResponse:
    # 503 if db_path None ; finding id from path ; body {status}
    # 400 if status not in _ALLOWED_STATUS
    # with request_conn: resolve finding → 404 if missing ; read old status
    #   if new == old: return 200 {id, status} WITHOUT writing an event (no-op)
    #   transaction():
    #     UPDATE retro_findings SET status=?, updated_at=? WHERE id=?
    #     INSERT INTO events(document_id, event_type, payload_json, created_at)
    #       VALUES (NULL, 'retro_finding_status_changed',
    #               json.dumps({finding_id, old_status, new_status}), now)
    # broadcast(); return {"id": id, "status": new}
```

```python
Route("/retro-findings/{finding_id:int}/status",
      post_retro_finding_status, methods=["POST"]),
```

### Surfacing — `project_page.py` + `project.html`

In `project_page`, after resolving the project, call `list_findings(conn, proj["id"])` and pass the rows to the template. In `project.html` add a "Process findings" section: each finding shows title, feature tag, a recurrence badge when `recurrence_count > 0`, the evidence/change, and status buttons (open / actioned / deferred / rejected). Terminal findings (actioned/rejected) can be de-emphasised or behind a toggle — keep open/recurring ones prominent.

**The status buttons are net-new client JS**: `project.html` today has no `fetch`/POST logic, only an EventSource that calls `location.reload()` on any broadcast. So a successful status POST (which broadcasts) already reloads the page — the button handler should just `fetch()` the endpoint and rely on that reload, *not* issue its own, to avoid a double refresh. This is genuine new work, not a trivial template tweak.

### Tests

- Read model ordering: recurring findings sort before non-recurring; among non-recurring, oldest `created_at` first; deterministic id tie-break.
- Status update happy path: POST sets status; GET `/retro-findings` then excludes an actioned/rejected finding; an `events` row is written with the right type and payload.
- No-op status change: POST the status the finding already has → 200 and **no** new `events` row.
- Status 404 unknown finding; 400 invalid status; 503 db not configured; broadcast fires on a real change.
- Project page renders the panel: a seeded finding's title appears; a recurring one shows the badge (assert on rendered HTML, as `project_page_test.py` does).

### MR chain

One MR titled `feat(retro-findings-capture): phase 2`, chained on Phase 1.

## QC

Before each commit, run the full gate from `CLAUDE.md` § "QA / quality control" and ensure all pass: `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, `uv run pytest`. Follow whatever `CLAUDE.md` says at implementation time. Note the deployed service is a separate `uv tool` install — after merging, redeploy per `CLAUDE.md` § "Running the deployed service" (a migration counts as a code change: restart; no new dependency, so no reinstall needed).

## Checklist

### Phase 1: Store & contract

- Add migration `0004_retro_findings.sql` (retro_runs, retro_findings, indexes, schema_version=4).
- Add a `db_test.py` assertion that `migrate()` reaches version 4 and the two tables exist (confirm the file exists first).
- Create `web/retro_findings.py` with `post_retro_findings` (validate-before-txn, resolve project or 404, replace-by-run, copy run.feature into findings, broadcast).
- Add `get_retro_findings` (project param or 400/404; return open+deferred with ids and recurrence_count of children of any status).
- Validate `recurs_from` (DB read inside request_conn, pre-txn): must reference a finding in this project and NOT in the run being replaced — else 400.
- Register both routes in `app.py`.
- Write `web/retro_findings_test.py`: round trip, idempotent re-post, recurrence round trip, self-run recurs_from→400, cross-project recurs_from→400, read filter, 404/400/503, broadcast.
- Run the full QC gate; open one MR `feat(retro-findings-capture): phase 1`.

### Phase 2: Surface & triage

- Create `storage/retro_findings.py` with `FindingRow` + `list_findings()` (recurring-first, oldest-open ordering, id tie-break).
- Add `post_retro_finding_status` to `web/retro_findings.py` (validate status, no-op writes no event, update + audit to events, broadcast); register the route.
- Load findings in `project_page.py` and render the "Process findings" panel in `project.html` with recurrence badge and status buttons (fetch-only; rely on the existing SSE reload, no double refresh).
- Write read-model tests (`storage/retro_findings_test.py`) and endpoint/status + project-page tests; cover the events-audit row, the no-op (no event), and read-filter-after-status-change.
- Run the full QC gate; open one MR `feat(retro-findings-capture): phase 2` chained on Phase 1.
