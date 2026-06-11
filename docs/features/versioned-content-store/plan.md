# versioned-content-store

## Overview

F1 evolves the existing filesystem walker (`storage/walker.py`) into a content-aware importer that stores each document's content in the DB as versioned, structured sections — cutting a new version only when content actually changes, keyed by a stable logical identity rather than the file path. It is purely additive: rendering still serves from disk (the iframe), the inbox query is unchanged, and feature-skills is untouched. Three MRs: **(1)** a pure, corpus-tested section parser + per-type manifests, no DB; **(2)** the schema (a `document_versions` table + a `logical_key`), the importer that gates versions/events on content equality, and a backfill migration that moves uniqueness off `source_path` while preserving existing read-state/responses; **(3)** bringing the tracker into the model as an opaque body and proving the importer idempotent across the whole real corpus.

## Key technical decisions

1. **Parsing lives in a new pure module `storage/doc_content.py`; the walker calls into it**
  The parser and manifests are pure functions with no DB dependency, so they live in their own module and can be fully unit-tested against the corpus in Phase 1 before any schema exists. This is decomposition, not a parallel walker — `walker.py` remains the sole orchestrator (it imports `doc_content` in Phase 2). Uses the stdlib `html.parser`, consistent with the existing `_MetaParser` / `_TrackerParser` — **no new dependencies**.
  ```python
  # storage/doc_content.py
  from dataclasses import dataclass
  from typing import Literal

  @dataclass(frozen=True)
  class Section:
      key: str       # the section's id attribute
      body: str      # inner HTML, re-serialised deterministically

  @dataclass(frozen=True)
  class ParsedContent:
      shape: Literal["sections", "opaque"]
      sections: tuple[Section, ...]   # opaque -> exactly one Section(key="", body=)

  @dataclass(frozen=True)
  class ManifestSpec:
      shape: Literal["sections", "opaque"]
      expected_keys: tuple[str, ...] = ()       # informational, for observability
      repeated_prefixes: tuple[str, ...] = ()   # e.g. ("phase-",) for plan

  def parse_content(html: str, spec: ManifestSpec) -> ParsedContent: ...
  def manifest_for(doc_type: str) -> ManifestSpec: ...
  ```
2. **Three manifest shapes; only context/requirements/plan are section-parsed**
  Per the requirements, the model carries three shapes. **Ordered sections** (context, requirements) — fixed key set, enumerating all template sections incl. optional ones like `design-notes`. **Repeated/optional** (plan) — one-or-more `phase-N` sections plus optional `qc`/`checklist`, expressed via `repeated_prefixes`. **Opaque whole-document** (the `features` tracker and every `*-feedback` type) — stored as a single `Section(key="", body=…)`, never decomposed. The section parser extracts the ordered `<section id>` elements inside `<main class="document">`; the header and the comment-rail/popover/footer chrome are outside the captured sections and fall away naturally. For opaque docs the body is the full file text (feedback docs have no `<main>` at all).
3. **Canonical JSON serialisation defines both storage and content-equality (byte-fidelity, not normalisation)**
  `content_json` is the serialised `ParsedContent`. Equality (the version/event gate) is byte-equality of this canonical JSON. The guarantee is **determinism over identical input bytes**: the same source HTML always re-serialises to the same JSON, so a literal no-op re-save produces identical JSON (→ no version) and any authored content change produces different JSON (→ exactly one version), satisfying the requirements invariant. This is deliberately **byte-fidelity-conservative, not semantic normalisation**: attribute order and in-tag whitespace are preserved as authored (via `get_starttag_text()`), so a resave that reorders an attribute or reflows whitespace *inside* a tag would cut a (harmless) spurious version. Chosen over normalisation because it never *swallows* a real edit and agents regenerate whole docs from templates, so byte-different-but-equal resaves are rare; full HTML normalisation is real complexity for little gain (signed off, review round 1).
  **Entity handling is load-bearing:** the parser must run with `convert_charrefs=False` and re-emit entity/char refs in `handle_entityref`/`handle_charref`. With the default (`True`), `html.parser` decodes entities into `handle_data`, which both corrupts the stored body for F2's eventual rendering *and* makes `&amp;amp;` compare equal to a bare `&amp;` — two genuinely different docs colliding. The existing `_MetaParser`/`_TrackerParser` dodge this (they read only attributes/title), so there's no codebase precedent to copy.
  ```python
  def serialise(content: ParsedContent) -> str:
      # compact JSON; the stored string is also the equality key
      return json.dumps(
          {"shape": content.shape,
           "sections": [{"key": s.key, "body": s.body} for s in content.sections]},
          separators=(",", ":"), ensure_ascii=False,
      )
  ```
4. **Logical identity is a derived `logical_key` string column (NULL-safe, API-shaped)**
  Add `logical_key TEXT` (UNIQUE after backfill) + `instance INTEGER` to `documents`. The key is a deterministic string the importer derives from the path and a future API supplies directly — same key both ways. A string (not a composite unique index over `project_id`/`feature_id`/`type`) avoids SQLite's "NULLs are distinct" trap for the tracker's null `feature_id`, and matches the templates' existing `docId` convention. `instance` is the feedback `N`; `1` for singletons.
  ```python
  # feature is None for the tracker -> "-"
  def logical_key(project: str, feature: str | None, doc_type: str, instance: int) -> str:
      return f"{project}/{feature or '-'}/{doc_type}/{instance}"
  # context:     myproj/myfeat/context/1
  # feedback:    myproj/myfeat/requirements-feedback/2
  # tracker:     myproj/-/features/1
  ```
  Needs a `feedback_instance(rel_path) -> int` helper alongside the existing `feedback_type()` (which today discards the `N`). **The canonical identity is the string**, not the requirements' descriptive tuple `(project, feature, phase, "feedback", N)`: a future API contracts to the same string form so importer and API can't disagree on the key.
5. **Archival is a status change on the same row — keyed by logical identity, not path**
  Today moving a doc into `.feedback-archive/` produces a *new* row at the new path plus a `missing` on the old — two rows for one logical document, which would collide on the new unique key. F1 fixes the semantic: `_process_file` looks up the document by `logical_key` (path-independent), so a moved file matches its existing row and just updates `source_path` + `status`. Two concrete edits this forces in `walker.py`, both currently keyed on `source_path`: the document lookup (`walker.py:289-292`) *and* the post-INSERT id fetch (`walker.py:342-344`, `SELECT id ... WHERE source_path=?`) — use `last_insert_rowid()` / lookup-by-logical-key instead.
  Ordering invariant: reconcile is a second loop *after* the file loop (`walker.py:415`) and keys "unseen" on `source_path NOT IN seen_paths` (`walker.py:421`). Because the moved row's `source_path` is updated to the now-present archived path *within the same transaction* before reconcile runs, the row is correctly skipped (not marked missing). The plan asserts and tests this; a future refactor must not move reconcile to run per-file or key on anything else. Edge case: if an active file and its archived copy are *both* on disk during one walk (same `logical_key`), the logical-key lookup prevents a unique-violation, but the final state would otherwise depend on `rglob` order — so apply a deterministic precedence: **active wins over archived**. The live corpus has zero such collisions today; this is a guard against the latent case.
6. **F1 leaves `content_html` NULL; the F2 seam is a version accessor**
  **Trap:** `doc_view.doc_raw` serves `content_html` *in preference to* the file on disk (`doc_view.py`: `if row["content_html"]: return HTMLResponse(...)`). Populating it in F1 would silently change the render source — violating "rendering untouched". So F1 leaves `content_html` alone and exposes the F2 content-access seam as a read accessor over the version table. F2 later decides whether to route rendering through this accessor or populate `content_html`.
  ```python
  # storage/versions.py
  def current_content(conn, document_id: int) -> ParsedContent | None: ...   # latest version, parsed back
  def record_version(conn, document_id: int, content: ParsedContent, actor: str, now: str) -> int: ...
  ```
7. **Version/event gating, and seeding version 1 on first sight**
  The mtime+size gate stays as a cheap pre-filter, but gains one clause. The full condition: **re-read if `status == "missing"` OR mtime/size changed OR there is no current version** (the first two are today's behaviour at `walker.py:298-304`; the third is new, so the first post-migration walk seeds every existing doc). On a content pass: parse → serialise → compare to the current version's `content_json`. **Opaque docs (feedback + tracker) are versioned from Phase 2 too**, not deferred — otherwise they'd stay unversioned and the gate would re-read them every walk; Phase 3 then only adds the tracker's *dual-representation* proof, not the versioning mechanism.
    - **Brand-new doc:** insert row, record version 1, emit `created`.
    - **Existing doc, no version yet (seed):** record version 1, emit **no** event, don't increment `updated` — it's a backfill seed, not a change.
    - **Existing doc, content differs:** record version *n+1*, emit the existing `updated`/`archived`/`reactivated` event, bump the matching counter.
    - **Existing doc, content identical:** update mtime/size metadata so the gate re-passes next time, but record **no** version and emit **no** event. This is the headline win — no-op re-saves go quiet.
  Versions track content, not status: archival/missing transitions cut no version; reactivation-with-changed-content does.
  ```python
  cur = current_content(conn, doc_id)      # None if unversioned
  new = parse_content(html, manifest_for(doc_type))
  if cur is None:
      record_version(conn, doc_id, new, actor="importer", now=now)   # seed, silent
  elif serialise(cur) != serialise(new):
      record_version(conn, doc_id, new, actor="importer", now=now)   # +1, with event
      emit_change_event(...)                                          # existing event path
  ```
8. **Backfill migration: derive keys, dedupe collisions preserving FKs, then add the unique index**
  The schema columns + version table land via SQL migration `0003` (plain DDL — the runner splits naively on `;`, so no triggers/semicolon-literals). The *backfill* needs path parsing and collision logic, which SQL can't do, so it runs once in Python, guarded to no-op once every row has a `logical_key`.
  **Wiring (corrected):** in `web/app.py` the migrate connection is opened, migrated, and *closed* in its own block (`app.py:49-51`) before the walk worker — which runs on a *separate* connection in a thread — is even created (`app.py:58`). So the backfill runs on that same short-lived connection, between `migrate(conn)` and `conn.close()` (`app.py:50-51`); it is a one-time idempotent op, not threaded into the walk.
  Steps: (a) compute `logical_key` for every row from its `source_path` using the same derivation the importer uses; (b) resolve collisions (pre-existing active/archived/missing duplicates of one logical doc) by keeping the survivor = highest `(status_rank, id)` and **repointing** `read_state`, `synthesis_responses`, and `comments` from losers to the survivor; (c) delete losers (whose now-empty `read_state`/`synthesis_responses`/ `comments` would otherwise cascade-delete — `foreign_keys=ON` at `db.py:47` — which is why repoint precedes delete, all in one transaction); (d) drop the old `idx_documents_source_path` unique index and `CREATE UNIQUE INDEX IF NOT EXISTS` on `logical_key`.
  **Conflict policy on repoint** (a plain INSERT-OR-IGNORE is underspecified): `read_state` (PK `document_id`) keeps `MAX(last_read_at)` across survivor+losers (lexical compare, safe per `now_iso`'s contract); `synthesis_responses` (PK `document_id, item_num`) is survivor-wins on a per-`item_num` conflict, with any loser answer discarded — an accepted, documented loss confined to the rare duplicate-row case (the live corpus has zero collisions).
  ```python
  # storage/versions.py
  def backfill_logical_keys(conn) -> None:
      """One-time, idempotent. No-op if every row already has a logical_key.
      Runs on the migrate connection in app.py (between migrate() and conn.close())."""
      # status_rank: active=2, archived=1, missing=0; survivor = max((rank, id))
      # repoint read_state (MAX last_read_at) / synthesis_responses (survivor-wins) / comments,
      # delete losers, drop idx_documents_source_path,
      # CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_logical_key_unique ...
  ```

## File structure

### New files

- `feature_skills_webapp/storage/doc_content.py` — pure parser + manifests + canonical serialisation (Phase 1).
- `feature_skills_webapp/storage/doc_content_test.py` — parser/manifest unit tests, corpus-driven (Phase 1).
- `feature_skills_webapp/storage/migrations/0003_versioned_content.sql` — version table + `logical_key`/`instance` columns + non-unique index (Phase 2).
- `feature_skills_webapp/storage/versions.py` — `record_version`, `current_content`, `backfill_logical_keys` (Phase 2).
- `feature_skills_webapp/storage/versions_test.py` — version accessors + backfill/dedupe tests (Phase 2).

### Modified files

- `feature_skills_webapp/storage/walker.py` — import `doc_content`/`versions`; logical-key lookup; content-gated version+event; seed-on-first-sight; archival-as-status-change; `feedback_instance` helper (Phase 2); opaque tracker versioning (Phase 3).
- `feature_skills_webapp/storage/walker_test.py` — new tests for the above (Phases 2–3).
- `web/app.py` — the lifespan migrate block (`app.py:49-51`): call `backfill_logical_keys(conn)` between `migrate(conn)` and `conn.close()` (Phase 2). The walk kick is at `app.py:60` on a separate worker connection — the backfill must *not* wait for it.

### Explicitly untouched in F1

- `web/doc_view.py` — rendering stays disk-based; `content_html` left NULL (see decision 6).
- `storage/inbox.py` — the inbox query is unchanged; it keeps reading the same events.

## Phase 1 — Section parser + per-type manifests

### What's built

The pure `storage/doc_content.py`: `parse_content(html, spec)`, `manifest_for(doc_type)`, the `Section`/`ParsedContent`/ `ManifestSpec` dataclasses, and `serialise(content)`. No DB, no walker changes. Section parsing isolates the **direct** `<section id>` children of `<main class="document">` (a nested `<section>` inside a body stays part of that body, opaque — capture only top-level sections) and re-serialises each section's inner HTML deterministically: `get_starttag_text()` for start tags, plus data/endtag re-emission, with **`convert_charrefs=False`** and entity/char-ref re-emission (decision 3). Opaque docs return a single `Section(key="", body=full_html)`.

### Key logic

- Manifests: `context`/`requirements` = ordered (enumerate all template section ids incl. optional); `plan` = repeated `phase-` + optional `qc`/`checklist`; `features` and any `*-feedback` type = opaque.
- Determinism contract: identical input *bytes* yield byte-identical `serialise()` output (what Phase 2's gate relies on). Not semantic normalisation — attribute order / in-tag whitespace are preserved (decision 3).
- Entity faithfulness: `&amp;amp;` and bare `&amp;` must *not* collapse to equal, and bodies must round-trip — hence `convert_charrefs=False`.
- Graceful classification: a section-parsed doc that has no `<main class="document">` (or zero sections) is reported as such (a sentinel/empty result the importer can flag), not an exception.

### Tests

- Corpus test: iterate every `*.html` under the real dev-store; assert context/requirements/plan parse to non-empty ordered sections whose keys are a subset of the manifest (plan: ≥1 `phase-N`); assert feedback/features classify as opaque. (Point it at `~/.claude/feature-docs`; skip cleanly if absent so CI without the store still passes.)
- Determinism: `serialise(parse(x)) == serialise(parse(x))`; a real section edit yields different serialisation; a *between-element* whitespace change is byte-different and may differ (the gate is conservative — we explicitly do *not* assert reflow-equality, since the mechanism is byte-fidelity, not normalisation).
- Entity round-trip: a body containing `&amp;amp;` survives parse→serialise distinct from one containing bare `&amp;`.
- Chrome exclusion: comment-rail/popover/footer markup never appears in any parsed section body; the `<header class="doc-header">` (a non-`<section>` child of `<main>`) is excluded.
- Fixtures mirror the real templates (reuse the `<main class="document">` + `<section id>` shape), plus a malformed/no-main fixture and a nested-section fixture.

### MR chain

One MR titled `feat(versioned-content-store): phase 1 — section parser + manifests`.

## Phase 2 — Versioned content model, importer + logical identity

### What's built

Migration `0003` (version table + `logical_key`/`instance` columns + non-unique `logical_key` index). `storage/versions.py` (`record_version`, `current_content`, `backfill_logical_keys`). The walker grows into the importer: logical-key derivation + lookup, the content-gated version/event logic (decision 7), archival-as-status-change (decision 5), and the `feedback_instance` helper. The backfill runs once at startup. Rendering untouched.

### Schema (0003)

```sql
ALTER TABLE documents ADD COLUMN logical_key TEXT;
ALTER TABLE documents ADD COLUMN instance INTEGER NOT NULL DEFAULT 1;

CREATE TABLE document_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_num INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (document_id, version_num)
);
CREATE INDEX idx_document_versions_document ON document_versions(document_id);
CREATE INDEX idx_documents_logical_key ON documents(logical_key);

INSERT INTO schema_version (version) VALUES (3);
```

The UNIQUE index on `logical_key` is created by `backfill_logical_keys` (after dedupe), not in the SQL — so the migration can't fail on pre-existing collisions.

### Importer changes (`walker.py`)

- Compute `doc_type` + `instance` + `logical_key` for each file; look the document up by `logical_key` (replacing the `WHERE source_path=?` lookup at `walker.py:289-292`), and fix the post-INSERT id fetch at `walker.py:342-344` (also keyed on `source_path` today) to use `last_insert_rowid()` / logical-key — so moved files reconcile onto one row (decision 5), with active-beats-archived precedence.
- Gate (full condition): skip read only if `status != "missing"` **and** mtime+size unchanged **and** a current version exists.
- Apply the decision-7 version/event matrix; stamp `actor="importer"`; version opaque docs (feedback + tracker) here too.
- Reconcile invariant: keep reconcile keying on the post-update `source_path` (`walker.py:421`) so a moved row isn't marked missing — assert with a test.
- Un-ingested observability: a section-parsed doc that fails to parse (no `<main>`/zero sections) is counted in `WalkSummary` (e.g. a new `unparsed` field) and logged — never aborts the walk, never silently vanishes.

### Tests

- `versions.py`: record/read round-trip; version numbering; `current_content` returns the latest.
- Importer invariant: fresh walk seeds version 1 with no spurious event; a byte-identical re-save cuts no version and emits no event (`summary.updated == 0`); a real edit cuts exactly one version + one `updated` event.
- Archival reconciles onto one row: move a doc to `.feedback-archive/` → same document id, status `archived`, no duplicate row, no `missing` event.
- Backfill: seed a pre-0003-shaped DB (rows with `source_path`, plus a contrived active+missing duplicate pair sharing a logical doc, with `read_state`/ `synthesis_responses` on the loser) → after backfill, one survivor row, FKs repointed (read-state + responses preserved), unique index present, second run is a no-op.
- Logical key + `feedback_instance`: unit tests incl. tracker (`proj/-/features/1`) and feedback `N` extraction.
- Rendering untouched: `content_html` stays NULL after a walk; `doc_raw` still serves from disk (existing `doc_view` tests stay green).

### MR chain

One MR titled `feat(versioned-content-store): phase 2 — versioned content model + importer`. Depends on Phase 1.

## Phase 3 — Tracker ingestion + full-corpus cutover proof

### What's built

Opaque versioning already lands in Phase 2 (decision 7), so Phase 3 is the **dual-representation proof and the full-corpus cutover guarantee**: confirm the `features` tracker is both opaque-versioned *and* still row-extracted into the `features` table, then prove the importer idempotent across the whole real corpus (feedback + archived included).

### Key logic

- `_apply_tracker_rows` is untouched (rows still extracted; the `shipped` event still fires on done-transitions) and runs alongside the opaque version recorded for the same `features.html` doc.
- No feedback/tracker doc is ever section-parsed; they always serialise as `shape="opaque"`.

### Tests

- Tracker dual representation: walking `features.html` both records an opaque version and populates `features` rows; a tracker edit that only changes a note cuts one version; re-walk cuts none.
- Single-change isolation: seed the full corpus, mutate one section of one doc, re-import → exactly one new version on that doc, zero new versions elsewhere.
- Full-corpus idempotency: import the entire real dev-store, then re-import → second run cuts zero versions and emits zero events (the cutover guarantee), feedback + archived docs included.
- Existing tracker/shipped-event tests stay green.

### MR chain

One MR titled `feat(versioned-content-store): phase 3 — tracker ingestion + cutover proof`. Depends on Phase 2.

## QC

There is no `CLAUDE.md` in this repo; follow the toolchain in `pyproject.toml` / `README.md`. Before each commit run, and ensure clean:

```bash
uv run ruff format .
uv run ruff check .
uv run ty check .
uv run pytest          # xdist + pytest-socket; per-worker DB
```

F1 adds **no runtime dependencies**, so no tool reinstall is needed — but the long-running `uv tool` service won't reflect code changes until restarted (`systemctl --user restart feature-skills-webapp`) if you want to exercise it live. Each phase is one MR; the implementing agent checks items off below as it goes and pauses for re-review if the approach deviates materially.

## Checklist

### Phase 1: Section parser + manifests

- Create `storage/doc_content.py` with `Section`, `ParsedContent`, `ManifestSpec` dataclasses.
- Implement `manifest_for(doc_type)`: ordered (context/requirements), repeated+optional (plan), opaque (features + `*-feedback`).
- Implement `parse_content` extracting *direct* `<section id>` children of `<main class="document">` (nested sections stay in the body), excluding chrome + header; opaque → single empty-key section.
- Parse with `convert_charrefs=False` and re-emit entity/char refs so bodies round-trip and `&amp;amp;` ≠ bare `&amp;`.
- Implement deterministic `serialise(content)` (canonical JSON, the byte-fidelity equality key — not normalisation).
- Graceful classification when a section-parsed doc has no `<main>`/zero sections (sentinel, not exception).
- Write `doc_content_test.py`: corpus parse, determinism (no reflow-equality assertion), entity round-trip, chrome+header exclusion, malformed + nested-section fixtures.
- QC clean (ruff/ty/pytest); open Phase 1 MR.

### Phase 2: Versioned content model, importer + identity

- Add migration `0003_versioned_content.sql` (version table, `logical_key`/`instance` columns, non-unique indexes, schema_version 3) — plain DDL only.
- Add `logical_key()` (canonical string) + `feedback_instance()` helpers.
- Create `storage/versions.py`: `record_version`, `current_content`.
- Implement `backfill_logical_keys`: derive keys; dedupe collisions repointing read_state (MAX last_read_at) / synthesis_responses (survivor-wins) / comments before deleting losers; drop `idx_documents_source_path`; create UNIQUE `logical_key` index; idempotent.
- Wire `backfill_logical_keys(conn)` into `app.py` between `migrate(conn)` and `conn.close()` (not the worker connection).
- Rework `_process_file`: lookup by `logical_key`; fix the post-INSERT id fetch (`walker.py:342`); archival-as-status-change on one row with active-beats-archived precedence.
- Preserve the reconcile-keys-on-source_path invariant (`walker.py:421`) so a moved row isn't marked missing.
- Implement the content gate (status==missing OR changed OR unversioned): seed v1 silently; cut v(n+1)+event only on real change; no-op stays silent; version opaque docs too.
- Add un-ingested observability (new `WalkSummary` field + log) for section-parse failures.
- Write `versions_test.py` + walker tests: invariant, archival-onto-one-row, backfill dedupe preserves+merges FKs (loser gone, survivor has union), `content_html` stays NULL.
- QC clean (ruff/ty/pytest); open Phase 2 MR.

### Phase 3: Tracker dual-representation + cutover proof

- Confirm `features.html` is both opaque-versioned (Phase 2 mechanism) and still row-extracted via untouched `_apply_tracker_rows` (shipped event intact).
- Confirm no feedback/tracker doc is ever section-parsed (always `shape="opaque"`).
- Tests: tracker dual-representation (version + rows); single-change isolation; full-corpus idempotent re-import (feedback + archived).
- Confirm existing tracker/shipped-event and doc_view tests stay green.
- QC clean (ruff/ty/pytest); open Phase 3 MR.
