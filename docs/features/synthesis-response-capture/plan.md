# synthesis-response-capture

## Overview

Let a developer answer a synthesis (feedback) doc inside the webapp and have the response stored server-side, retrievable by the agent, and surfaced as an "Awaiting your input" inbox category — replacing the copy-to-clipboard / paste-into-chat round-trip. The work is four phases, each one MR: (1) make the walker index feedback docs (today it skips them — they carry no `feature-doc-type` meta tag); (2) HTTP write/read endpoints over the pre-existing `synthesis_responses` table; (3) the inbox category; (4) the in-doc submit affordance. No schema migration is needed — the table has existed unused since migration 0001. The feature-skills *skill-side* change that consumes the read endpoint is out of scope (it lives in `skill-integration-parallel`); the clipboard stays as a fallback until then.

## Key technical decisions

1. **Index feedback docs via a filename fork in `_process_file`, not `identity_for`**
  `identity_for` already returns an identity for feedback docs — a depth-3 path `proj/feat/requirements-feedback-1.html` matches the active-feature-doc branch, and a depth-4 `.feedback-archive/` path matches the archived branch. The skip happens in `_process_file`, where `parse_doc_html` returns `None` for a doc with no meta tag and the file is counted as an error and dropped. So the change is a typing fork there: try the meta tag first, fall back to a filename-derived synthetic type.
  ```python
  import re

  _FEEDBACK_RE = re.compile(r"^(?P<phase>[a-z]+)-feedback-\d+$")

  def feedback_type(rel_path: Path) -> str | None:
      """Synthetic doc type for a feedback doc, e.g. 'requirements-feedback'. None if not one."""
      m = _FEEDBACK_RE.match(rel_path.stem)
      return f"{m.group('phase')}-feedback" if m else None
  ```
  In `_process_file`, replace the `parse_doc_html` call with direct `_MetaParser` use so the title is still captured when the type comes from the filename:
  ```python
  mp = _MetaParser()
  mp.feed(html_content)
  doc_type = mp.doc_type or feedback_type(rel_path)
  if doc_type is None:
      log.debug("Skipping %s: no meta tag and not a feedback doc", abs_path)
      summary.errors += 1
      return
  parsed = ParsedDoc(doc_type=doc_type, title=mp.title)
  ```
  Type string is `<phase>-feedback` (e.g. `requirements-feedback`) so `humanise_type()` renders "Requirements feedback" with no new label-table entry. Trailing `-N` is dropped — multiple rounds of the same phase share a type. The `phase` group is deliberately narrow (`[a-z]+`) — it matches today's `requirements`/`plan`/`review` phases; a future hyphenated phase would need the regex widened. Define the shared suffix once as a module constant (`FEEDBACK_SUFFIX = "-feedback"`) so the walker's type-building and the inbox's `LIKE '%-feedback'` match (Phase 3) stay in agreement rather than duplicating a magic string.
2. **"Awaiting input" is doc-level: a synthesis doc with no `synthesis_responses` rows**
  Per the requirements' round-1 decision, awaiting = active synthesis doc (`type LIKE '%-feedback'`) that has never been submitted (no rows in `synthesis_responses`). No item-set parsing. Submission is recorded by writing a row per item *including empty-string responses* (an empty answer means "agree" — it must still count as submitted), so the mere existence of any row flips the doc out of "awaiting".
3. **Replace-on-submit; parent-frame document id authoritative**
  The write endpoint is keyed by the path's `document_id` (the webapp controls the URL); the body's path-style `doc` field is ignored for routing. **All validation (shape, integer item keys, 1 MB cap) runs before `BEGIN IMMEDIATE`** so a bad key returns a clean 400 rather than an exception escaping mid-transaction (which `transaction()` would roll back into a 500). After validation, a submit does a delete-then-insert of the doc's full response set inside one `transaction()`, so a regenerated doc whose item set changed can't leave stale rows.
  ```python
  with transaction(conn):
      conn.execute("DELETE FROM synthesis_responses WHERE document_id=?", (doc_id,))
      for item, text in responses.items():        # text may be "" (= agree)
          conn.execute(
              "INSERT INTO synthesis_responses (document_id, item_num, response, routine_flag, updated_at) "
              "VALUES (?, ?, ?, NULL, ?)", (doc_id, int(item), text, now))
      for item, comment in routine_flags.items():
          conn.execute(
              "INSERT INTO synthesis_responses (document_id, item_num, response, routine_flag, updated_at) "
              "VALUES (?, ?, NULL, ?, ?)", (doc_id, int(item), comment, now))
  ```
4. **Read endpoint keyed by source path**
  The agent knows a doc by the absolute dev-store path it wrote (`~/.claude/feature-docs/<proj>/<feat>/<phase>-feedback-<N>.html`) — the same value stored in `documents.source_path`. So the read endpoint takes that path as a query param and looks the document up by it, sidestepping the path→numeric-id gap. Unknown path → 404; known but never submitted → 200 with `submitted: false` (an unambiguous not-yet state, distinct from a missing doc).
5. **⚠ Submit by reading the iframe DOM directly — diverges from the requirements' postMessage bridge**
  The requirements chose a `postMessage` bridge, assuming the doc-view shell can't reach into the rendered doc. But `doc.html` renders the doc in a **same-origin, non-sandboxed** `<iframe src="/doc/{id}/raw">`, so the parent frame *can* read `iframe.contentDocument` directly and assemble the payload itself — reusing the same selectors the feedback template's "Copy responses" button uses:
  ```javascript
  const doc = frame.contentDocument;
  const responses = {};
  doc.querySelectorAll('.your-thoughts textarea').forEach(ta => {
    responses[ta.dataset.item] = ta.value.trim();
  });
  const routine_flags = {};
  doc.querySelectorAll('.tier-routine .flag-btn.active').forEach(btn => {
    const item = btn.dataset.item;
    routine_flags[item] = doc.querySelector(
      `.tier-routine li[data-item="${item}"] .flag-input textarea`).value.trim();
  });
  await fetch(`/doc/${docId}/synthesis-response`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ responses, routine_flags }),
  });
  ```
  **Why this is better:** it needs *zero* change to feature-skills' `feedback-template.html` (the bridge needed a new message handler there), and it works retroactively on feedback docs that already exist — the same philosophy as filename-indexing over a meta tag. Both approaches couple to the template's internals; this couples to its DOM classes (`.your-thoughts textarea[data-item]`, `.tier-routine .flag-btn.active`) instead of a message protocol. **This is the one place the plan departs from approved requirements — flagged for confirmation in review.** If rejected, fall back to the postMessage bridge plus a template-side responder (a cross-repo change).
  **Two robustness guards** (the iframe loads after the shell, and a doc could in principle render unexpected markup): gate the Submit button on the iframe's `load` event so the DOM is parsed before any read, and *refuse to POST when zero `.your-thoughts textarea` elements are found* — that means this isn't the expected feedback markup, and a silent empty submit would be worse than a no-op with a visible "couldn't read this doc" state.
6. **Live inbox update reuses the existing broadcaster**
  After a successful submit, the endpoint calls `request.app.state.broadcaster.broadcast()` (the same fan-out `/events` serves and the walk worker already uses). The endpoint runs on the event loop, so the call is in-thread — none of the cross-thread care the walk worker needs. An open inbox tab refetches and the doc drops out of "Awaiting your input".

## File structure

### New files

- `feature_skills_webapp/web/synthesis.py` — the write (`post_synthesis_response`) and read (`get_synthesis_response`) route handlers.
- `feature_skills_webapp/web/synthesis_test.py` — endpoint tests.

### Modified files

- `storage/walker.py` — `feedback_type()` helper; typing fork in `_process_file`. *(Phase 1)*
- `storage/walker_test.py` — feedback-doc indexing tests. *(Phase 1)*
- `storage/inbox.py` — `awaiting_input()`; `Inbox.awaiting_input` field; `is_empty`; `build_inbox`; exclusion in `new_since_last_visit`. *(Phase 3)*
- `storage/inbox_test.py` — awaiting-input + exclusion tests. *(Phase 3)*
- `web/app.py` — register the two synthesis routes. *(Phase 2)*
- `web/doc_view.py` — pass an `is_synthesis` flag + post URL to the shell template; exclude `*-feedback` docs from `siblings()`. *(Phase 4)*
- `web/templates/doc.html` — "Submit response" button + DOM-read POST script. *(Phase 4)*
- `web/templates/_inbox_body.html` — "Awaiting your input" section. *(Phase 3)*

## Phase 1 — Index synthesis docs

### What's built

The walker learns to index feedback docs. `feedback_type()` + the typing fork in `_process_file` (Decision 1). Feedback docs then become first-class `documents` rows with type `<phase>-feedback`, status active (depth-3) or archived (depth-4 `.feedback-archive/`), emitting the usual created/updated/ archived/missing/reactivated events. No user-visible change yet.

### Files touched

`storage/walker.py`, `storage/walker_test.py`.

### Tests

- `feedback_type()` unit cases: `requirements-feedback-1` → `requirements-feedback`; `plan-feedback-2` → `plan-feedback`; `requirements`, `context`, `features` → `None`.
- A walk over a docs tree with a meta-tag-less `requirements-feedback-1.html` at depth-3 indexes it active with the synthetic type (previously it was an error/skip).
- The same file under `.feedback-archive/` indexes as archived.
- Negative depth-4 guard: a depth-4 path *not* under `.feedback-archive/` is still skipped by `identity_for` — the typing fork must not widen which paths get indexed.
- A genuinely typeless, non-feedback doc is still skipped and counted in `errors` (no regression).
- `humanise_type("requirements-feedback")` renders "Requirements feedback".

### MR chain

One MR titled `feat(synthesis-response-capture): phase 1 — index feedback docs`.

## Phase 2 — Capture & read endpoints

### What's built

A new `web/synthesis.py` with two handlers, registered in `app.py`. Both follow the `admin_mark_read` conventions: per-request connection via `request_conn`, writes inside `transaction()`, `now_iso()` timestamps, `503` when `db_path is None`.

```python
# POST /doc/{document_id:int}/synthesis-response
async def post_synthesis_response(request: Request) -> JSONResponse:
    # 503 if db unconfigured
    # body = await request.json(); 400 if not a dict / responses|routine_flags not dicts
    #   / any item key non-integer / any value > 1 MB
    # 404 if no documents row with that id
    # replace-on-submit (Decision 3); broadcaster.broadcast() after commit
    # 200 -> {"document_id": id, "items_written": n}

# GET /synthesis-response?path=<abs source_path>
async def get_synthesis_response(request: Request) -> JSONResponse:
    # 503 if db unconfigured; 400 if path param missing
    # 404 if no documents row with that source_path
    # rebuild payload from rows:
    #   responses     = {str(item): response     for rows where routine_flag IS NULL}
    #   routine_flags = {str(item): routine_flag for rows where routine_flag IS NOT NULL}
    # 200 -> {"doc": path, "submitted": bool(rows), "responses": ..., "routine_flags": ...}
```

The 1 MB cap is per response/flag value, checked before the write (Decision: defensive, not restrictive — round 1). Trust boundary: the server binds `127.0.0.1` only; no auth.

### Files touched

`web/synthesis.py` (new), `web/synthesis_test.py` (new), `web/app.py`.

### Tests

- POST then GET round-trips the payload; empty-string responses are stored and come back (and mark the doc submitted).
- Re-POST with a different item set replaces (no stale rows).
- POST 404 unknown id; 400 malformed body; 400 non-integer item key; 400 over-size value; 503 when db unconfigured.
- GET 404 unknown path; 200 `submitted: false` for a known-but-unsubmitted doc.
- A successful POST broadcasts (register a queue on the broadcaster and assert it receives a message).

### MR chain

One MR titled `feat(synthesis-response-capture): phase 2 — capture & read endpoints`.

## Phase 3 — Awaiting-input inbox

### What's built

A fourth inbox category. `awaiting_input()` mirrors `new_since_last_visit`'s join shape:

```python
def awaiting_input(conn, project_id: int | None = None) -> list[InboxCard]:
    sql = (
        "SELECT d.id AS document_id, d.type AS doc_type, p.name AS project, f.slug AS feature, "
        "  (SELECT MAX(e.created_at) FROM events e WHERE e.document_id = d.id) AS last_activity "
        "FROM documents d "
        "JOIN projects p ON d.project_id = p.id "
        "JOIN features  f ON d.feature_id = f.id "
        "WHERE d.status = 'active' AND d.type LIKE '%-feedback' "
        "  AND NOT EXISTS (SELECT 1 FROM synthesis_responses sr WHERE sr.document_id = d.id)"
    )
    # optional project filter; ORDER BY last_activity DESC, document_id DESC
```

The `JOIN features` is safe: feedback docs are always feature-scoped (the depth-3/depth-4 identity always carries a `feature`), so the inner join never drops them. The `'%-feedback'` pattern is built from the shared `FEEDBACK_SUFFIX` constant (Decision 1), used in both inbox queries rather than hard-coded twice.

`Inbox` gains an `awaiting_input: list[InboxCard]` field; `is_empty` and `build_inbox` include it. `new_since_last_visit` excludes unsubmitted synthesis docs so they don't double-list (Decision 2 / round-1 UX call). The fragment is ANDed into the existing `WHERE d.status = 'active' AND EXISTS (…)` block *before* the optional `project_id` append and the `ORDER BY` (the SQL is built by string concatenation, so the insertion point matters):

```sql
AND NOT (d.type LIKE '%-feedback'
         AND NOT EXISTS (SELECT 1 FROM synthesis_responses sr WHERE sr.document_id = d.id))
```

`_inbox_body.html` gets an "Awaiting your input" section rendered first (most urgent), with clickable `document_id` cards like "New since last visit". The live-update on submit (Decision 6) already works via the existing `EventSource` client.

### Files touched

`storage/inbox.py`, `storage/inbox_test.py`, `web/templates/_inbox_body.html`.

### Tests

- An active feedback doc with no responses appears in `awaiting_input` and is absent from `new_since`.
- After a submission (any row, incl. empty response), it leaves `awaiting_input`.
- An archived feedback doc never appears in `awaiting_input`.
- `is_empty` accounts for the new category.
- Template renders the section with the right card link; project filter still works.

### MR chain

One MR titled `feat(synthesis-response-capture): phase 3 — awaiting-input inbox`.

## Phase 4 — Submit from the webapp

### What's built

The in-doc submit affordance (Decision 5, pending confirmation). `doc_view.doc_shell` computes `is_synthesis = row["type"].endswith(FEEDBACK_SUFFIX) and row["status"] == "active"` and passes it plus the post URL into `doc.html`. When set, the doc bar shows a **Submit response** button, enabled on the iframe's `load` event; its script reads the same-origin iframe's DOM, assembles `{responses, routine_flags}`, refuses to POST if no `.your-thoughts textarea` is found (Decision 5 guard), otherwise POSTs to `/doc/{id}/synthesis-response` and reflects success / failure in the button. The feedback doc's own "Copy responses" button stays as a fallback. No feature-skills change.

Also in this phase: **exclude `*-feedback` docs from `siblings()`** — a one-line predicate (`AND type NOT LIKE '%-feedback'`, or the equivalent in the Python filter) on the active-docs query — so feedback docs stay out of the context→requirements→plan prev/next nav. Starting them out; trivial to revisit.

### Files touched

`web/doc_view.py`, `web/templates/doc.html`, `web/doc_view_test.py`.

### Tests

- `doc_shell` sets `is_synthesis` true for an active `*-feedback` doc, false for a `plan`/`context` doc and for an archived feedback doc.
- The shell renders the Submit button (and the post URL) only when `is_synthesis`.
- `siblings()` omits an active feedback doc from a feature's prev/next nav (feedback docs don't appear as siblings of plan/requirements).
- The DOM-read + POST path is exercised by the round-trip test from Phase 2; the button wiring is verified by template assertion (JS behaviour isn't unit-tested here, consistent with the no-JS-test convention elsewhere).

### MR chain

One MR titled `feat(synthesis-response-capture): phase 4 — submit from the webapp`.

## QC

No `CLAUDE.md` in this repo; follow the README's Development section before each commit:

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run ty check .
```

This feature adds no new runtime dependencies (stdlib `re`/`json` plus the existing Starlette + broadcaster), so the `uv tool install --reinstall` + service-restart dance (README "Updating after a dependency change") is *not* required. Tests use the `temp_db` fixture and `TestClient(create_app(db_path=…, docs_root=…))`; feedback-doc fixtures are meta-tag-less HTML named `*-feedback-N.html`.

## Checklist

### Phase 1: Index synthesis docs

- Add `FEEDBACK_SUFFIX` constant, `_FEEDBACK_RE`, and `feedback_type(rel_path)` to `walker.py`.
- Replace the `parse_doc_html` call in `_process_file` with direct `_MetaParser` use + filename fallback (Decision 1).
- Add `feedback_type` unit tests and active/archived indexing tests; assert non-feedback typeless docs still skip and the negative depth-4 (non-`.feedback-archive`) path stays skipped.
- QC (pytest, ruff, ty); open MR `feat(synthesis-response-capture): phase 1`.

### Phase 2: Capture & read endpoints

- Create `web/synthesis.py` with `post_synthesis_response` (validation, replace-on-submit, 1 MB cap, broadcast).
- Add `get_synthesis_response` keyed by `source_path` with the `submitted` flag.
- Register both routes in `web/app.py`.
- Write `web/synthesis_test.py`: round-trip, replace, error codes, not-submitted, broadcast.
- QC; open MR `feat(synthesis-response-capture): phase 2`.

### Phase 3: Awaiting-input inbox

- Add `awaiting_input()` to `inbox.py` (using `FEEDBACK_SUFFIX`); add the `Inbox.awaiting_input` field; update `is_empty` and `build_inbox`.
- Exclude unsubmitted synthesis docs from `new_since_last_visit` — AND the fragment into the existing `WHERE` before the `project_id` append and `ORDER BY`.
- Add the "Awaiting your input" section to `_inbox_body.html` (rendered first, clickable cards).
- Tests: appears-when-unsubmitted, excluded-from-new, leaves-on-submit, archived-never, `is_empty`, template render.
- QC; open MR `feat(synthesis-response-capture): phase 3`.

### Phase 4: Submit from the webapp

- Compute `is_synthesis` in `doc_shell`; pass it + the post URL to the template.
- Exclude `*-feedback` docs from `siblings()` (one-line predicate).
- Add the Submit button + DOM-read POST script to `doc.html` (gated on `is_synthesis`; enable on iframe `load`; refuse POST if no textareas found).
- Tests: `is_synthesis` truth table; button rendered only when set; `siblings()` omits feedback docs.
- QC; open MR `feat(synthesis-response-capture): phase 4`.
