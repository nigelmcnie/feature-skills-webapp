# agent-submission-api

## Overview

Add logical-key HTTP endpoints that let a local agent create/update the agent-authored doc types (context, requirements, plan, feedback) and read back content, comments, and synthesis — addressed by logical identity (project / feature / doc type / instance), never by file path. The write path reuses the storage primitives F1 established (`logical_key`, `record_version`, `current_content`, `manifest_for`, the `transaction()` discipline) so it converges on the *same* document row as the existing filesystem walker. The work is two MRs: Phase 1 is the write path (a storage `submit_document()` core plus a `PUT` handler with a dry-run mode); Phase 2 is the read round-trips (content, comments, integrate, synthesis, manifest). All endpoints are additive — the path-keyed endpoints and the walker stay as compatibility.

## Key technical decisions

1. **Addressing: logical-identity path components, reusing the walker's key**
  Each operation identifies a doc by its four identity components as URL path segments (each is slash-free: project/feature slugs, a doc-type slug, an integer instance; project-level docs use `-` for the feature, matching `logical_key()`). The API derives the storage key with the *same* function the walker uses — never a re-implementation — so file-import and API-submit land on one row. `PUT` is the create-or-update verb (full replacement fits PUT semantics).
  ```python
  # app.py routes (Phase 1 adds the PUT; Phase 2 adds the GETs + integrate)
  Route("/api/documents/{project}/{feature}/{doc_type}/{instance:int}", put_document, methods=["PUT"])
  Route("/api/documents/{project}/{feature}/{doc_type}/{instance:int}", get_document)            # P2
  Route("/api/documents/{project}/{feature}/{doc_type}/{instance:int}/comments", get_document_comments)            # P2
  Route("/api/documents/{project}/{feature}/{doc_type}/{instance:int}/comments/integrate", post_document_comments_integrate, methods=["POST"])  # P2
  Route("/api/documents/{project}/{feature}/{doc_type}/{instance:int}/synthesis", get_document_synthesis)          # P2
  Route("/api/manifests/{doc_type}", get_manifest)                                                # P2

  # storage key — imported, not re-derived:
  from feature_skills_webapp.storage.walker import logical_key
  key = logical_key(project, None if feature == "-" else feature, doc_type, instance)
  ```
2. **Shared write core in `storage/documents.py`**
  A new storage module holds the create-or-update logic, tested without HTTP (the codebase splits pure storage from web glue — walker vs routes, doc_render vs doc_view). It reuses `versions.record_version`/`current_content` and the walker's identity + upsert helpers; the walker's `_process_file` is **not** refactored in this feature (keeps blast radius small — the convergence guarantee comes from sharing `logical_key`, pinned by a test). A submit is a full replacement; a new version is cut only when the serialised content changes (same gate as the walker), and the matching `created`/`updated` event is emitted so the inbox treats an API change like a file change.
  ```python
  @dataclass(frozen=True)
  class SubmitResult:
      document_id: int
      logical_key: str
      version_num: int     # latest version after the write
      created: bool        # True if the document row was created by this call
      changed: bool        # False when content was byte-identical (no new version)

  class SubmitError(Exception):
      """Validation failure — surfaced by the web layer as 400."""

  def submit_document(
      conn: sqlite3.Connection, *,
      project: str, feature: str | None, doc_type: str, instance: int,
      content: ParsedContent, actor: str, now: str,
  ) -> SubmitResult:
      """Create-or-update a document by logical_key. Caller wraps in transaction().
      - upsert project (+ feature, when feature is not None) via the shared helpers
      - find existing row by logical_key; INSERT if absent (status='active',
        source_path=NULL, instance, metadata title derived), else UPDATE metadata
      - three write states, mirroring walker._process_file:
          * INSERT: create row, then record_version (v1) + 'created' event
          * existing row WITH a current version: record_version + 'updated' event only
            when serialise(current) != serialise(content); identical -> no version, no event
          * existing row with NO version yet (current_content is None — e.g. a row a prior
            failed submit left, or future seams): seed the first version silently, no event
            (matches walker.py's `cur is None` seed branch)
      """
  ```
  The walker's `_upsert_project` / `_upsert_feature` are promoted to public `upsert_project` / `upsert_feature` and shared (the only edit to `walker.py` — rename plus its two call sites).
  **Convergence is row-level, not byte-level.** A file-imported doc and an API submit of the same identity resolve to one row via `logical_key`. But the walker stores sections in *authored* order while the API stores them in *manifest* order (Decision 3), and `serialise()` is order-sensitive — so the first API submit onto an imported row may cut one extra version even for "the same" content. That's harmless (`render_section_doc` reorders to manifest order regardless). Bodies are also stored exactly as supplied (no `_SectionParser` re-serialisation), so the guarantee is one *row*, not byte-identity — the version gate is internally consistent because it always compares the stored prior serialise to the new one.
3. **Content building & validation against the manifest**
  The web layer turns the request into a `ParsedContent` and validates it before any DB work — mirroring the pre-transaction validation in `web/synthesis.py`/`comments.py`. Section docs submit a `sections` map and store in **manifest order** (deterministic `serialise()` → stable version-on-change); opaque docs (`*-feedback`) submit a single `body`. Unknown section keys are rejected; missing keys are tolerated (renderer already tolerates gaps).
  ```python
  WRITABLE_SECTION_TYPES = {"context", "requirements", "plan"}
  MAX_BODY_BYTES = 1024 * 1024  # mirror the existing per-value guard

  def validate_writable(doc_type: str, feature: str | None, instance: int) -> None:
      """Raise SubmitError unless this identity is writable in v1:
      - doc_type in WRITABLE_SECTION_TYPES, or doc_type.endswith('-feedback')
        (the tracker 'features' type and unknowns are rejected — tracker ops are deferred)
      - feature is not None (all writable types are feature-scoped)
      - instance == 1 unless doc_type endswith '-feedback'
      """

  def build_content(doc_type: str, sections: dict[str, str] | None, body: str | None) -> ParsedContent:
      """Validate against manifest_for(doc_type) and build ParsedContent, else SubmitError.
      - opaque (manifest.shape == 'opaque'): require `body`, forbid `sections`
        -> ParsedContent('opaque', (Section('', body),))
      - sections: require `sections`, forbid `body`; every key in manifest.expected_keys
        (else SubmitError); build Section(k, sections[k]) in manifest order
      - enforce MAX_BODY_BYTES on each body string
      """
  ```
4. **Manifest exposure — the full spec, single-sourced**
  `GET /api/manifests/{doc_type}` serialises the webapp's own `ManifestSpec` so an agent authors against it without embedding a copy. It returns the shape, ordered `(key, label)` sections, and the repeated-section prefixes (so a plan author knows `phase-*` sections are dynamic). Required/optional is not modelled (the spec has no such field and the renderer tolerates gaps) — out of scope.
  ```python
  # GET /api/manifests/plan ->
  {
    "doc_type": "plan",
    "shape": "sections",
    "sections": [{"key": "overview", "label": "Overview"},
                 {"key": "key-decisions", "label": "Key technical decisions"}, "…"],
    "repeated_prefixes": ["phase-"]
  }
  # GET /api/manifests/requirements-feedback -> {"doc_type": "...", "shape": "opaque",
  #   "sections": [], "repeated_prefixes": []}
  ```
5. **Metadata for path-less docs; response shape; codes**
  An API-created row has no file, so: status is `active`, `source_path` is `NULL`, and the inbox title is derived as `f"{feature} — {humanise_type(doc_type)}"` (`humanise_type` lives in `storage/inbox.py`; no `<title>` tag to read); the API path gates on content equality, so no `size` is stored. (If a file later appears at the same logical key, the walker's update branch overwrites the derived title with the file's `<title>` — acceptable convergence behaviour.) A submit returns enough to close the loop. Status codes mirror the existing endpoints.
  ```python
  # PUT response 200:
  {"logical_key": "feature-skills-webapp/agent-submission-api/requirements/1",
   "document_id": 42, "version_num": 3, "url": "/doc/42", "created": false, "changed": true}
  # dry_run=true -> 200 {"valid": true} (no write); validation failure -> 400 {"error": "..."}
  # codes: 400 bad JSON / SubmitError, 404 unknown doc (reads), 503 db not configured
  #   note: a non-integer instance never reaches the handler — the {instance:int} route
  #   convertor matches digits only, so a bad instance is a router 404, not a 400.
  ```
  **actor** comes from the body (`actor`), defaulting to `"agent"` when absent — attribution shouldn't be a hard failure. **dry_run** is the query param `?dry_run=true`: it runs `validate_writable` + `build_content` and returns the verdict without opening a transaction.
6. **Reads resolve the row by logical key, then reuse existing query shapes**
  The Phase 2 read handlers resolve `logical_key → document_id` with a single lookup, then run the *same* SQL and emit the *same* JSON shape as the path-keyed handlers in `web/comments.py` / `web/synthesis.py` — only the doc-resolution differs. A known doc with no data yet returns `200` with an empty/`submitted:false` body (matches today's GET synthesis); an unknown logical key returns `404`. Comment-integrate takes `{ids: [...]}` in the body and reuses the existing status-update + event.

## File structure

### New files

- `feature_skills_webapp/storage/documents.py` — `SubmitResult`, `SubmitError`, `validate_writable`, `build_content`, `submit_document`.
- `feature_skills_webapp/storage/documents_test.py` — unit tests for the write core (no HTTP), including the convergence and reconcile-safety tests.
- `feature_skills_webapp/web/submit.py` — HTTP handlers: `put_document` (P1); `get_document`, `get_manifest`, `get_document_comments`, `post_document_comments_integrate`, `get_document_synthesis` (P2).
- `feature_skills_webapp/web/submit_test.py` — TestClient tests for the endpoints.

### Modified files

- `feature_skills_webapp/web/app.py` — register the new routes (PUT in P1; reads/manifest in P2).
- `feature_skills_webapp/storage/walker.py` — promote `_upsert_project`/`_upsert_feature` to public `upsert_project`/`upsert_feature`; update their two internal call sites. No change to the gate or reconcile logic.

## Phase 1 — Submit documents by logical identity

### What's built

The write path end-to-end: `storage/documents.py` (`submit_document` + validation + content building) and `web/submit.py`'s `put_document`, registered as `PUT /api/documents/{project}/{feature}/{doc_type}/{instance:int}`, with a `?dry_run=true` mode. An agent can author and revise any of the four doc types without writing a file; the doc renders in the inbox and a version is cut only on real change.

### Files touched

New `storage/documents.py`, `storage/documents_test.py`, `web/submit.py`, `web/submit_test.py`; modified `web/app.py`, `storage/walker.py`.

### Key shapes

```python
async def put_document(request: Request) -> JSONResponse:
    # 503 if app.state.db_path is None
    # path params: project, feature, doc_type, instance (int)
    # body = await request.json()  (400 on failure / non-dict)
    # actor = body.get("actor") or "agent"; dry_run = query "dry_run" in {"1","true"}
    # try: validate_writable(doc_type, feat, instance); content = build_content(doc_type,
    #          body.get("sections"), body.get("body"))   except SubmitError -> 400
    # if dry_run: return {"valid": True}
    # with request_conn(app) as conn, transaction(conn):
    #     result = submit_document(conn, project=..., feature=feat, doc_type=..., instance=...,
    #                              content=content, actor=actor, now=now_iso())
    # app.state.broadcaster.broadcast()
    # return {logical_key, document_id, version_num, url: f"/doc/{id}", created, changed}
```

### Tests

- `documents_test.py` — `build_content`: section doc stores manifest-ordered; opaque (`*-feedback`) stores single body; unknown section key raises; `body`+`sections` mismatch per shape raises; oversize body raises. `validate_writable`: rejects `features` + unknown types, rejects `feature=None`, rejects `instance≠1` for non-feedback.
- `submit_document` (on a **backfilled connection** — connect + migrate + `backfill_logical_keys`, the `temp_conn` idiom in `walker_test.py`, so the `logical_key` unique index exists and a duplicate-row bug actually fails): creates row (status active, source_path NULL, derived title, version 1, `created` event); update cuts a new version + `updated` event; resubmitting identical content cuts no version and emits no event (`changed=False`); the `cur is None` seed branch (row present, no version) records v1 silently with no event.
- **Convergence (keystone):** walk-import a file doc, then `submit_document` with the same identity → assert **one row, same `document_id`** (the convergence property). Then assert version behaviour explicitly: an API submit of *different* content increments the version; submitting content that serialises identically to the current version does not. (Because authored vs manifest order differ, a file→API submit of "the same" text may legitimately be +1 version — assert on the row, not a vacuous version count.)
- **Reconcile-safety (keystone):** create an API doc (no file) via `submit_document`, run `walk(conn, docs_root, reconcile=True)` over a root that doesn't contain it → assert status stays `active` and no `missing` event (pins the `NULL NOT IN (...)` exclusion against a future `COALESCE(source_path,'')`).
- `submit_test.py` — PUT happy path (200 + all response fields, doc fetchable at `/doc/{id}`); `?dry_run=true` writes nothing; 400s (bad JSON, unknown section key, non-writable `features` type, `feature=-`, `instance=2` for requirements); 503 db-not-configured; broadcast fired (register a queue, assert non-empty — as in `synthesis_test.py`).

### MR chain

One MR titled `feat(agent-submission-api): phase 1 — submit by logical key`.

## Phase 2 — Read round-trips by logical identity

### What's built

The read side, completing the author → comment → read → revise → integrate loop by logical key: `get_document` (current content), `get_manifest`, `get_document_comments`, `post_document_comments_integrate`, `get_document_synthesis` — all in `web/submit.py`, registered in `app.py`. Path-keyed endpoints remain untouched.

### Files touched

Extended `web/submit.py` + `web/submit_test.py`; modified `web/app.py` (four read routes + manifest route).

### Key shapes

```python
# GET /api/documents/{p}/{f}/{t}/{i}  -> resolve doc row by logical_key (404 if none)
{"logical_key": "...", "document_id": 42, "doc_type": "requirements", "shape": "sections",
 "sections": [{"key": "problem", "body": "……"}, "…"], "version_num": 3, "url": "/doc/42"}

# GET   /api/documents/{p}/{f}/{t}/{i}/comments   -> same JSON shape as web/comments.get_comments
# POST  /api/documents/{p}/{f}/{t}/{i}/comments/integrate  body {"ids": [1,2]} -> {"integrated": n}
# GET   /api/documents/{p}/{f}/{t}/{i}/synthesis  -> same shape as web/synthesis.get_synthesis_response
#       (known doc, no rows -> 200 submitted:false; unknown key -> 404)
```

A small shared helper resolves `logical_key → document_id` (returns `None` → 404). Comment/synthesis bodies reuse the existing SELECTs and JSON shapes verbatim; only resolution differs from the `?path=` handlers.

### Tests

- `submit_test.py` — content read round-trips a Phase-1 PUT (sections back in manifest order); manifest read for a section type (shape + labels + `repeated_prefixes`) and an opaque type; comments read + integrate by logical key (integrated rows drop from the active set); synthesis read returns empty `submitted:false` for a fresh doc and the populated set after a POST; unknown logical key → 404 on each read; 503 db-not-configured.

### MR chain

One MR titled `feat(agent-submission-api): phase 2 — read round-trips by logical key`.

## Verification

Machine-runnable acceptance commands. The new tests fail loudly when the feature is absent (the endpoints 404 / the imports fail), so the suite is the primary gate.

- Lint/format/types: `uv run ruff format --check . && uv run ruff check . && uv run ty check .`
- Full suite (xdist + pytest-socket, per-worker DB): `uv run pytest` — expect all pass, including the new `documents_test.py` / `submit_test.py`.
- Targeted (the feature's own tests): `uv run pytest feature_skills_webapp/storage/documents_test.py feature_skills_webapp/web/submit_test.py`
- End-to-end against the running service (optional; after `systemctl --user restart feature-skills-webapp` per `CLAUDE.md`): `curl -fsS -X PUT "http://127.0.0.1:8800/api/documents/feature-skills-webapp/agent-submission-api/requirements/1?dry_run=true" -H 'Content-Type: application/json' -d '{"sections":{"problem":"<h2>Problem</h2><p>x</p>"}}'` → expect `{"valid": true}`; then `GET /api/manifests/requirements` → expect the section list.

## QC

Run the full QC gate from `CLAUDE.md` § "QA / quality control" before each commit — `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, `uv run pytest` — all must pass. Follow whatever `CLAUDE.md` says at implementation time. New endpoints are code-only (no dependency change), so a `systemctl --user restart feature-skills-webapp` is enough to see them in the running service during manual verification.

## Checklist

### Phase 1: Submit by logical identity

- Promote `_upsert_project`/`_upsert_feature` to public `upsert_project`/`upsert_feature` in `walker.py`; update the two internal call sites.
- Add `storage/documents.py` with `SubmitResult`, `SubmitError`, and `validate_writable()` (doc-type allowlist incl. `*-feedback`; feature-scoped; instance rule).
- Implement `build_content()`: manifest-ordered sections for section docs, single opaque body for `*-feedback`, reject unknown keys, enforce 1 MB per body.
- Implement `submit_document()`: reuse `logical_key` + upserts; find-or-create row (active, source_path NULL, derived title via `inbox.humanise_type`); handle all three write states (INSERT→v1+`created`; existing+versioned→`record_version`+`updated` on change; existing+unseeded `cur is None`→silent v1); return `SubmitResult`.
- Add `web/submit.py` `put_document`: path params, JSON body, `actor` default, `?dry_run`, `SubmitError`→400, 503/400 codes, `transaction()`, broadcast, response with id/version/url/created/changed.
- Register the `PUT /api/documents/{project}/{feature}/{doc_type}/{instance:int}` route in `app.py`.
- Write `storage/documents_test.py` (on a backfilled connection so the unique index exists): `build_content`/`validate_writable` cases; create/update/no-op-on-identical/`cur is None`-seed; **convergence** (file-import ↔ API-submit → same row id; version behaviour under identical vs changed content); **reconcile-safety** (path-less doc never marked missing).
- Write `web/submit_test.py`: PUT happy path, dry-run writes nothing, the 400 cases, 503 db-not-configured, broadcast fired.
- Run the full QC gate; open MR `feat(agent-submission-api): phase 1 — submit by logical key`.

### Phase 2: Read round-trips by logical identity

- Add a shared `logical_key → document_id` resolver (returns `None` for 404) in `web/submit.py`.
- Implement `get_document` (current content → sections + url; 404 on unknown key).
- Implement `get_manifest` at `/api/manifests/{doc_type}` (shape + ordered labels + repeated_prefixes; opaque types return empty sections).
- Implement `get_document_comments` and `post_document_comments_integrate` (`{ids:[...]}`), reusing the existing comment SELECT/UPDATE + event.
- Implement `get_document_synthesis` (reuse the existing response shape; known-empty → `submitted:false`).
- Register the four read routes + the manifest route in `app.py`.
- Extend `web/submit_test.py`: content round-trip, manifest (section + opaque), comments read + integrate, synthesis (empty + populated), unknown-key 404s, 503 db-not-configured.
- Run the full QC gate; open MR `feat(agent-submission-api): phase 2 — read round-trips by logical key`.
