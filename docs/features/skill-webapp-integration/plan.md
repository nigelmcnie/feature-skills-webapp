# skill-webapp-integration

## Overview

Wire the `feature-skills` review loop into the webapp. Two webapp phases give click-to-comment annotations a server-side home — a capture/read HTTP surface mirroring the shipped synthesis-response one, folding in the unbuilt `comment-capture` (#8) — and three skill phases retire the browser auto-opens and both clipboard handoffs, reading responses *and* comments over HTTP by the dev-store path the skills already hold, via non-blocking polling. The `comments` table already exists empty (migration 0001), so no migration is needed; the work is one new module (`web/comments.py`), small edits to the `doc-view` shell, a one-line addition to the `feature-skills` spine templates, and prose edits across the skill `SKILL.md` files.

**Repo note.** Phases 1–2 land in `feature-skills-webapp` (plus a one-line touch in the `feature-skills` templates, in Phase 1). Phases 3–5 are entirely in the `feature-skills` repo (`/home/nigel/src/nigelmcnie/feature-skills`) — the implementing agent works in that repo for those phases. Each phase is one MR.

## Key technical decisions

1. **Reuse the empty `comments` table; no migration**
  Defined in `0001_init.sql`, never written to. We are its first writer, exactly as `synthesis-response-capture` was for `synthesis_responses`. Use `status` values `'active'` and `'integrated'`; `integrated_at` is set only on the integrate transition.
  ```sql
  CREATE TABLE comments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      excerpt TEXT,
      text TEXT NOT NULL,
      status TEXT NOT NULL,        -- 'active' | 'integrated'
      created_at TEXT NOT NULL,
      integrated_at TEXT
  );
  ```
2. **Read comments from the iframe via `window.__fsComments`**
  The spine docs keep comments in a top-level `const comments = []` — a lexical binding, *not* a property of the iframe window, so `frame.contentWindow.comments` is `undefined`; and the rail DOM truncates the excerpt to 100 chars, so scraping is lossy. The fix (decided with Nigel at plan time, reversing the round-1 "no template change" intent): one backwards-compatible line in each `feature-skills` spine template exposes the array, and the same-origin `doc-view` shell reads it. This is the comment analogue of how synthesis submit reads `.your-thoughts textarea` from the iframe DOM.
  ```javascript
  // feature-skills: requirements-template.html + plan-template.html
  const comments = [];
  window.__fsComments = comments;   // ← added: expose for the doc-view shell

  // feature-skills-webapp: doc.html comment-submit handler
  const data = frame.contentWindow.__fsComments || [];
  const payload = data.map(c => ({ excerpt: c.excerpt, text: c.text }));
  await fetch(commentPostUrl, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ comments: payload }),
  });
  ```
3. **Replace-active-set on submit; integrated rows untouched**
  A submit deletes the doc's `active` rows and re-inserts the rail's current set as `active`, in one `BEGIN IMMEDIATE` transaction — the same replace-on-submit discipline synthesis uses. `integrated` rows are never touched, so history survives and a rail deletion propagates by omission (no delete endpoint). Each `text`/`excerpt` is length-capped at 1 MB before the transaction, mirroring `post_synthesis_response`.
  ```python
  with transaction(conn):
      conn.execute(
          "DELETE FROM comments WHERE document_id = ? AND status = 'active'",
          (doc_id,),
      )
      for c in comments:                       # c: {"excerpt": str|None, "text": str}
          conn.execute(
              "INSERT INTO comments (document_id, excerpt, text, status, created_at) "
              "VALUES (?, ?, ?, 'active', ?)",
              (doc_id, c.get("excerpt"), c["text"], now),
          )
      conn.execute(
          "INSERT INTO events (document_id, event_type, payload_json, created_at) "
          "VALUES (?, 'comment_submitted', ?, ?)",
          (doc_id, json.dumps({"count": len(comments)}), now),
      )
  ```
4. **Mark integrated by id, scoped to the doc**
  After the agent folds comments in, it marks exactly the ids it read (never a per-doc sweep), so a comment left mid-round isn't retired unread. The integrate endpoint is path-keyed (the identity the agent holds) and validates each id belongs to that document. Like submit, it writes a `comment_integrated` `events` row — the audit convention is a deliberate addition (synthesis logs no event; it only broadcasts), applied to both comment transitions.
  ```python
  with transaction(conn):
      for cid in ids:
          conn.execute(
              "UPDATE comments SET status = 'integrated', integrated_at = ? "
              "WHERE id = ? AND document_id = ? AND status = 'active'",
              (now, cid, doc_id),
          )
      conn.execute(
          "INSERT INTO events (document_id, event_type, payload_json, created_at) "
          "VALUES (?, 'comment_integrated', ?, ?)",
          (doc_id, json.dumps({"count": len(ids)}), now),
      )
  ```
5. **Endpoint identity mirrors synthesis: write by doc-id, read/integrate by path**
  The browser shell holds the document id (write); the agent holds the dev-store source path (read, integrate). This is exactly the `synthesis.py` split (`POST /doc/{id}/synthesis-response` vs `GET /synthesis-response?path=`). Same `503`-unconfigured / `404`-unknown-doc / `400`-malformed / `200`-ack contract.
  ```python
  # web/comments.py
  async def post_comments(request: Request) -> JSONResponse:           # /doc/{document_id:int}/comments
  async def get_comments(request: Request) -> JSONResponse:            # /comments?path=
  async def post_comments_integrate(request: Request) -> JSONResponse: # /comments/integrate  body {path, ids}

  # GET /comments?path=… response shape
  { "doc": "",
    "submitted": true,                # any comment row exists (active or integrated)
    "comments": [ {"id": 7, "excerpt": "…", "text": "…"} ] }   # active only, ORDER BY id
  ```
6. **Terminal signal: synthesis submit (or, for plan, the chat reply) — not a comment-only done-edge**
  Comments are *supplementary*. For `feature-requirements` and `feature-review` the round's terminal signal is the synthesis `submitted` flag; when it flips true the skill also fetches comments for the (co-located) spine doc by path. `feature-iterate` is the same edge but *two docs apart*: the poll trigger is the `review-feedback-<N>` synthesis, whereas the comments it must fetch live on the `requirements` and `plan` spine docs — so it fetches those spine paths, not the feedback doc's. `feature-plan` has no synthesis doc: it fetches comments from the endpoint at *each* human edge it already has — the Step 4 "go" reply, every Step 5 iterate round, and the Step 6 handoff — not just the first triage, since a comment can be left at any of them.
  So no dedicated comment-only polling done-edge is needed in v1 (round-1 deferral upheld). Note the read's `submitted` stays true forever once any comment has existed (decision 5), so it is *not* a "new comments waiting" signal — the skills key off the returned `comments` array length, not `submitted`.
7. **Skill-side: force-walk then poll every 5 s; clipboard as fallback**
  After writing a doc the skill triggers `POST /admin/discover` (so the walker indexes it), then polls every 5 s. A `404` means "not yet indexed" (keep waiting); a `200` with `submitted=false` means "indexed, awaiting the human". No hard ceiling — review can take a long time — but the skill emits a periodic "still waiting in the inbox" status and falls back to the clipboard-paste path if the developer gives up or the server is unreachable. Skills are markdown; this is written as a reusable convention block referenced by each skill.
  ```bash
  curl -fsS -X POST http://127.0.0.1:8800/admin/discover >/dev/null
  # then poll (pseudo): GET /synthesis-response?path=$ABS_PATH
  #   curl 404  → not indexed yet → sleep 5, retry
  #   200 submitted=false → awaiting human → sleep 5, retry
  #   200 submitted=true  → consume responses / routine_flags
  # unreachable / developer gives up → fall back to "Copy responses" paste
  ```

## File structure

### New files — `feature-skills-webapp`

- `feature_skills_webapp/web/comments.py` — comment write / read / integrate handlers.
- `feature_skills_webapp/web/comments_test.py` — endpoint tests (mirror `synthesis_test.py`).

### Modified — `feature-skills-webapp`

- `feature_skills_webapp/web/app.py` — register 3 routes.
- `feature_skills_webapp/web/doc_view.py` — `is_commentable` + `comment_post_url` in the shell context.
- `feature_skills_webapp/web/templates/doc.html` — comment Submit button + JS reading `__fsComments`.
- `feature_skills_webapp/web/doc_view_test.py` — assertions for the new shell context.

### Modified — `feature-skills` repo

- `feature/requirements-template.html`, `feature/plan-template.html` — `window.__fsComments = comments;` (Phase 1).
- `feature-context`, `feature-requirements`, `feature-plan`, `feature-review` `SKILL.md` — drop `google-chrome` (Phase 3).
- `feature-requirements`, `feature-review`, `feature-iterate` `SKILL.md` — synthesis over HTTP (Phase 4).
- `feature-requirements`, `feature-plan`, `feature-review`, `feature-iterate` `SKILL.md` — comments over HTTP (Phase 5).

## Phase 1 — Webapp — capture & read comments

### What's built

The comment write endpoint (doc-id-keyed, replace-active-set, 1 MB cap, `events` row, SSE broadcast), the path-keyed read endpoint (active comments only), the `doc-view` comment-submit affordance, and the one-line `window.__fsComments` exposure in the `feature-skills` spine templates (the sole cross-repo touch in this phase). After this phase a developer can submit comments from the webapp and they are retrievable over HTTP.

### Key details

- `post_comments`: validate body is an object with a `comments` list; each item needs a string `text` and optional string `excerpt`; reject non-strings / over-cap with `400`; `404` if the doc id is unknown; `503` if DB unconfigured. Replace-active-set per decision 3; `request.app.state.broadcaster.broadcast()` after commit.
- `get_comments`: keyed by `?path=`; `404` if no document has that `source_path`; returns active comments (decision 5 shape) with `submitted = bool(any comment rows)`.
- `doc_view.py`: gate commentability on the spine doc types.
- `doc.html` JS: enable and read the comment button only after the iframe `load` event (as the synthesis handler does), so `frame.contentWindow.__fsComments` exists before it's read; degrade to `[]` if absent.
- Validation (string-type + 1 MB cap) runs in a loop *before* `with transaction(conn):`, matching `post_synthesis_response`'s validate-then-BEGIN ordering.

```python
# doc_view.py — in doc_shell, alongside is_synthesis
COMMENTABLE_TYPES = {"requirements", "plan"}
is_commentable = (
    row["type"] in COMMENTABLE_TYPES
    and row["status"] == "active"
    and row["feature"] is not None
)
# ...added to the template context:
"is_commentable": is_commentable,
"comment_post_url": f"/doc/{doc_id}/comments",
```

```python
# app.py routes (added)
Route("/doc/{document_id:int}/comments", post_comments, methods=["POST"]),
Route("/comments", get_comments),
```

### Tests

- `comments_test.py`: submit writes active rows; re-submit replaces active set and leaves integrated rows; `400` on non-string text / over-cap / bad shape; `404` unknown doc; `503` unconfigured; an `events` row is written; broadcaster fires. Read returns active-only in id order, with `submitted`; `404` for unknown path; `"none yet"` (empty list, `submitted=false`) for an indexed doc with no comments.
- `doc_view_test.py`: `is_commentable` true for an active requirements/plan doc, false for synthesis, tracker (null feature), archived, and missing docs; `comment_post_url` present.
- Manual: open a requirements doc in `doc-view`, add a comment, click Submit, confirm via `GET /comments?path=`.

### MR chain

One MR titled `feat(skill-webapp-integration): phase 1 — capture & read comments`. Phase 1 spans both repos (the two `window.__fsComments` lines in `feature-skills`, the rest in `feature-skills-webapp`), so it is two MRs that complete the phase together. Merge order doesn't matter: the template line is backwards-compatible (a harmless extra global), and the webapp handler degrades to `[]` via `|| []` if the line hasn't shipped — so neither half breaks the other in isolation.

## Phase 2 — Webapp — integration state

### What's built

The integrate endpoint that marks specific comments integrated, so a later round's read returns only new comments. Small and independently testable; separate from Phase 1 because it's only exercised once a skill consumes comments (Phase 5).

### Key details

- `post_comments_integrate`: body `{path, ids}`; resolve `path`→document (`404` if unknown); validate `ids` is a list of ints (`400` otherwise); mark matching `active` rows integrated (decision 4); write a `comment_integrated` `events` row; return `{integrated: <count>}`. Ids not belonging to the doc or already integrated are no-ops (the `WHERE` guards).
- Confirm `get_comments` filters `status = 'active'` so integrated rows drop out.
- Register `Route("/comments/integrate", post_comments_integrate, methods=["POST"])`.

### Tests

- Integrate marks only the given ids; the active read then excludes them; integrated rows survive a subsequent active-set replace; ids from a different doc are rejected/ignored; `400` bad `ids`; `404` unknown path; `503` unconfigured.

### MR chain

One MR titled `feat(skill-webapp-integration): phase 2 — comment integration state`.

## Phase 3 — Skills — drop the Chrome opens

### What's built

Remove all five `google-chrome … &` invocations and replace each with a one-line pointer to the inbox (`http://127.0.0.1:8800`). No webapp dependency; independently shippable; can land before or after Phases 1–2. Entirely in the `feature-skills` repo.

### Files touched

- `feature-context/SKILL.md` (the `context.html` open).
- `feature-requirements/SKILL.md` (`requirements.html` draft open + `requirements-feedback-<N>.html` open).
- `feature-plan/SKILL.md` (`plan.html` draft open).
- `feature-review/SKILL.md` (`review-feedback-<N>.html` open).

### Tests

- None automated (skills are markdown). Verify by reading each edited step for coherence and grepping that no `google-chrome` invocation remains.

### MR chain

One MR (in `feature-skills`) titled `feat(skill-webapp-integration): phase 3 — drop Chrome opens`.

## Phase 4 — Skills — read synthesis responses over HTTP

### What's built

Rewrite the synthesis handoff in `feature-requirements` (Step 6/6b), `feature-review` (Step 8) and `feature-iterate` (Step 1): after writing the synthesis doc, force-walk then poll `GET /synthesis-response?path=<abs>` (decision 7), consuming `responses` / `routine_flags` on `submitted=true` instead of waiting for a pasted blob. The clipboard path stays documented as the unreachable / give-up fallback. Depends only on the already-shipped synthesis endpoint (Phases 1–2 not required).

### Key details

- Add the reusable "poll convention" block (decision 7) once and reference it from each skill, so the 5 s / 404-vs-waiting / fallback behaviour is defined in one place.
- The `path` is the dev-store absolute path the skill wrote (e.g. `~/.claude/feature-docs/<PROJECT>/<FEATURE>/requirements-feedback-<N>.html`), which is exactly `documents.source_path` — no normalisation.
- The returned `responses`/`routine_flags` shape is identical to the pasted blob's, so the downstream integration logic in each skill is unchanged — only the *acquisition* changes.

### Tests

- None automated. Verify by a dry run: write a synthesis doc, submit in the webapp, confirm the skill picks it up; confirm the fallback prose is coherent.

### MR chain

One MR (in `feature-skills`) titled `feat(skill-webapp-integration): phase 4 — synthesis over HTTP`.

## Phase 5 — Skills — read comments over HTTP

### What's built

The comment handoff across `feature-requirements`, `feature-plan`, `feature-review` and `feature-iterate`: fetch comments via `GET /comments?path=<spine-doc-abs>`, fold them in, then `POST /comments/integrate {path, ids}` for the ids consumed. Completes the clipboard retirement. Depends on Phases 1–2.

### Key details

- `feature-requirements` / `feature-review`: fetch comments for the spine doc(s) when the synthesis poll reports `submitted=true` (decision 6).
- `feature-plan`: no synthesis doc — fetch comments from the endpoint at *each* human edge, not just the first triage: the Step 4 "go" reply, every Step 5 iterate round, and the Step 6 handoff ("integrate any unprocessed feedback"). Each replaces the corresponding `{"doc": …, "comments": […]}` paste.
- `feature-iterate`: the poll trigger is the `review-feedback-<N>` synthesis, but comments live on the `requirements` / `plan` spine docs — so on the review-synthesis edge it fetches *those* spine paths (not the feedback doc's), then integrates.
- Mark integrated only the ids just folded in (decision 4); keep the "Copy comments" paste as the documented fallback.

### Tests

- None automated. Dry run: leave comments on a requirements doc, submit, confirm the skill reads and then integrates them (subsequent read returns empty).

### MR chain

One MR (in `feature-skills`) titled `feat(skill-webapp-integration): phase 5 — comments over HTTP`.

## QC

This repo has no `CLAUDE.md`; QC follows the README "Development" section. Before each webapp commit (Phases 1–2):

```bash
uv run ruff format . && uv run ruff check . && uv run ty check .
uv run pytest
```

No new runtime dependency is added, so the `uv tool install --reinstall` + service-restart dance (needed only on a runtime-dep change) does not apply here. Phases 3–5 (`feature-skills` repo) are markdown-only: QC is a careful read-through plus a manual dry run of the edited skill against a running webapp.

## Checklist

### Phase 1: Webapp — capture & read comments

- In `feature-skills`, add `window.__fsComments = comments;` after the `const comments = []` declaration in `feature/requirements-template.html` and `feature/plan-template.html`.
- Create `web/comments.py` with `post_comments` (doc-id-keyed; validate shape/strings/1 MB cap; replace-active-set in one `transaction()`; write a `comment_submitted` `events` row; broadcast after commit; 503/404/400/200).
- Add `get_comments` (path-keyed; active-only, id-ordered; `submitted` flag; 404 unknown path; 503 unconfigured).
- Register `POST /doc/{document_id:int}/comments` and `GET /comments` in `app.py`.
- In `doc_view.py`, compute `is_commentable` (active requirements/plan with a feature) and pass it + `comment_post_url` into the shell context.
- In `doc.html`, add a comment Submit button (shown when `is_commentable`) and JS that reads `frame.contentWindow.__fsComments` and POSTs it; reuse the synthesis-submit button states.
- Write `comments_test.py`: replace-active-set, 400/404/503, active-only read, events row, broadcast, "none yet" state.
- Extend `doc_view_test.py`: `is_commentable` true/false matrix and `comment_post_url`.
- QC (ruff/ty/pytest) + manual browser submit; open one MR.

### Phase 2: Webapp — integration state

- Add `post_comments_integrate` to `web/comments.py` (body `{path, ids}`; resolve path→doc; mark matching active rows integrated with `integrated_at`; write a `comment_integrated` events row; return count).
- Register `POST /comments/integrate` in `app.py`.
- Confirm `get_comments` excludes `integrated` rows.
- Tests: marks only given ids; active read excludes them; integrated survives a later active-set replace; cross-doc ids rejected; 400/404/503.
- QC; one MR.

### Phase 3: Skills — drop the Chrome opens

- In `feature-skills`, remove the `google-chrome` invocation in `feature-context`, `feature-requirements` (both), `feature-plan` and `feature-review`.
- Replace each with a one-line "it's in your inbox at `http://127.0.0.1:8800`" pointer.
- Grep that no `google-chrome` remains; read-through; one MR.

### Phase 4: Skills — synthesis over HTTP

- Write the reusable poll-convention block (force-walk → 5 s poll → 404-vs-waiting → give-up/unreachable fallback to clipboard).
- Rewrite `feature-requirements` Step 6/6b to poll `/synthesis-response?path=` instead of waiting for the responses paste; keep clipboard as fallback.
- Rewrite `feature-review` Step 8 likewise.
- Rewrite `feature-iterate` Step 1 to read from the endpoint rather than expecting a pasted blob.
- Dry-run verify; one MR.

### Phase 5: Skills — comments over HTTP

- In `feature-requirements` and `feature-review`, fetch spine-doc comments via `GET /comments?path=` when synthesis reports `submitted=true`, fold in, then `POST /comments/integrate {path, ids}`.
- In `feature-plan`, fetch comments from the endpoint at *each* human edge — the Step 4 "go" reply, every Step 5 iterate round, and the Step 6 handoff — not just the first triage; each replaces a comments paste.
- In `feature-iterate`, on the `review-feedback-<N>` synthesis edge fetch comments for the `requirements`/`plan` spine doc paths (not the feedback doc), then integrate.
- Keep "Copy comments" paste as the documented fallback everywhere; dry-run verify; one MR.
