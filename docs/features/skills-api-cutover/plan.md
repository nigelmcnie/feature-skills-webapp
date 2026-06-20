# skills-api-cutover

## Overview

The webapp's logical-key API (F3) and tracker mutation API (agent-submission-tracker-ops) are both shipped and unused by the skills. This feature is the adoption plus cleanup: rewrite the `feature-*` skills to author documents, read documents, and mutate the tracker through HTTP instead of writing/reading dev-store HTML files; flip the `features` table to be authoritative (retiring the walker's tracker parse); prove the API-authored DB state matches the file+walker path; then snapshot and delete the dev-store, walker, watcher, and legacy path-keyed endpoints. Work spans two repos — **feature-skills** (SKILL.md flows + export script) and **feature-skills-webapp** (walker flip, import CLI, parity tooling, deletions). Eight phases, each a single MR in its repo, sequenced so the file-based path keeps working until the final phase and the two irreversible/risky steps (the authority flip and the deletion) each sit behind a mechanical gate.

**Cross-repo merge order.** Because phases interleave repos, land them in this order: feature-skills P1 → P2 → P3, then feature-skills-webapp P4 (flip, gated on P3 merged) → P5 → P6, then feature-skills P7 (gated on P6 parity passing), then feature-skills-webapp P8 (deletion). Do not batch all skills MRs first — P4 depends on P3 being merged, and P7 depends on P6.

## Key technical decisions

1. **Skills author and read over HTTP from inside SKILL.md (curl), not new code**
  The skills are markdown instruction files, not a code library — the "rewrite" replaces file-write/file-read prose with `curl` calls against the local webapp. The authoring pattern is: fetch the manifest, assemble section bodies keyed by manifest key, `PUT` them. Documents are addressed by logical key `{project}/{feature}/{doc_type}/{instance}`.
  ```bash
  # fetch the section structure (keys + order) for a doc type
  curl -fsS http://127.0.0.1:8800/api/manifests/requirements

  # write a section doc by logical key (full replacement, manifest-keyed)
  curl -fsS -X PUT \
    http://127.0.0.1:8800/api/documents/$PROJECT/$FEATURE/requirements/1 \
    -H 'Content-Type: application/json' \
    -d '{"sections": {"problem": "<p>…</p>", "vision": "…"}, "actor": "agent"}'
  # -> {"logical_key":"…","document_id":N,"version_num":N,"url":"/doc/N","created":bool,"changed":bool}
  ```
  Opaque docs (feedback) send `{"body": "<html>"}` instead of `sections`. Validate before a destructive rewrite with `?dry_run=true` (returns `{"valid": true}`). No HTML templates, no file write, no walk wait — the write is the event.
2. **Tracker mutations use the typed endpoints; capture tolerates 409**
  The three tracker-writing skills call the shipped tracker API instead of editing `features.html`. Verbs and routes:
  ```bash
  # capture: create an available row (then PUT the context doc separately)
  curl -fsS -X POST http://127.0.0.1:8800/api/projects/$PROJECT/features/$FEATURE/capture \
    -H 'Content-Type: application/json' -d '{"notes": "one-line scope"}'
  #   200 (changed:true); 409 if the slug already exists (NOT idempotent) -> treat as already-captured

  # claim: available -> in_progress, owner required
  curl -fsS -X POST http://127.0.0.1:8800/api/projects/$PROJECT/features/$FEATURE/claim \
    -H 'Content-Type: application/json' -d '{"owner": "Nigel"}'   # 400 if owner empty

  # ship: in_progress -> done, outcome optional (omitted leaves notes unchanged)
  curl -fsS -X POST http://127.0.0.1:8800/api/projects/$PROJECT/features/$FEATURE/ship \
    -H 'Content-Type: application/json' -d '{"outcome": "Shipped. …"}'
  ```
  The `{feature}` path segment is the **real slug**, not the `-` project sentinel the documents API uses for project-level docs. Owner cell: `/feature-requirements` calls `claim` (sets owner); ship is in `/feature-review`. There is no "move"/"reopen".
3. **Authority flip = delete the tracker parse, gated by a mechanical "no writers left" check**
  Today `walker.walk()` calls `_apply_tracker_rows`, which re-derives the `features` table from `features.html` on every walk (last-writer-wins). Retiring that call makes the table authoritative. It is only safe once no skill writes `features.html` any more — otherwise a still-file-based claim is silently lost. The gate is a targeted, per-writer check — a blanket `grep features.html` is not usable (it matches argument-hints and the legitimate *read-only* tracker references in `feature-choice` and `feature-retro`, which never migrate, so it can never reach zero). For each of the three writers, confirm the file-write is gone *and* the API call is present:
  ```bash
  # For each writer (feature-context, feature-requirements, feature-review):
  #   (a) no tracker-FILE write remains — expect ZERO hits per file:
  grep -nE 'features\.html|<tr|feature-name|>\s*In Progress|>\s*Done' \
    ~/src/nigelmcnie/feature-skills/feature-context/SKILL.md \
    | grep -iE 'write|append|cp |edit|>>'        # any hit = still file-writing
  #   (b) the API call is present:
  grep -nE '/features/.*/(capture|claim|ship)' \
    ~/src/nigelmcnie/feature-skills/feature-context/SKILL.md   # expect the capture call
  # Read-only tracker references (feature-choice, feature-retro) are out of scope by design.
  ```
  Pinned by a keystone test (name must contain `keystone`/`anti_clobber` so the `-k` filter targets it): capture/claim via the API, run a walk, assert the row's status survives (red before the flip, green after). tracker-ops deliberately did not write this test — it lands here.
4. **Import CLI: lift `walk()` into a standalone entrypoint before deleting it**
  The walker only runs embedded in the server. A new thin CLI wraps the existing `walk()` so dev-store / repo HTML can be ingested into a DB without the server — the tool the final import uses and the ingestion path that survives deletion.
  ```python
  # feature_skills_webapp/cli.py
  def main(argv: list[str] | None = None) -> int:
      """Standalone importer: feature-skills-import --db PATH --docs-root DIR [--reconcile]."""
      args = _parse_args(argv)
      # open_db (storage/db.py) is a @contextmanager — it connects + migrates, then yields.
      with open_db(args.db) as conn:
          summary = walk(conn, args.docs_root, reconcile=args.reconcile)
      print(summary)                       # WalkSummary: created/updated/…/duration_ms
      return 0
  ```
  Register in `pyproject.toml` alongside the existing entry: `[project.scripts] feature-skills-import = "feature_skills_webapp.cli:main"`.
5. **Parity = normalised section comparison, with by-construction fields excluded**
  Both write paths converge on the same `logical_key` + version-on-change rows by design, but the stored serialiser is byte-sensitive, so parity compares *normalised section text* per logical key, not raw bytes. For each side, load the document's current content with `current_content(conn, document_id)` (`storage/versions.py`), join the two sides on `logical_key`, and compare `doc_diff.extract_text(section.body)` (the whitespace/tag-stripped extractor it already uses for diffs) per section key. Exclude fields that differ by construction: `source_path` (path vs NULL), `actor` (`importer` vs `agent`), the `metadata` size key, and event provenance. Pass conditions: every logical key matches under normalisation; the final import reports `created=0, updated=0`; zero documents render in `raw-fallback`; re-authoring this requirements doc via the API diffs clean — all against the redeployed service.
6. **Export `features.md` by merge, not render-from-scratch**
  The `features` table models neither intra-state row order nor the prose "Suggested order" section, so regenerating from scratch silently drops both. The export rewrites only the table-modelled rows in place and leaves row order + prose untouched — the same opaque-region instinct F1 uses for feedback bodies. Per-feature doc exports stay a straight render. The export repoint runs *after* the parity gate so the DB is proven before it's trusted as the source.
7. **Cut one skill at a time; keep the file path alive until the end**
  No in-skill dual-write. Skills move to the API one at a time, each verified before the next; the dev-store stays readable and the walker stays alive through Phases 1–7, so a regression is fixed in one skill rather than via a runtime fallback. Deletion is the last phase, behind the parity gate, and is made recoverable by a dated `tar.gz` snapshot.

## File structure

### feature-skills-webapp — new files

- `feature_skills_webapp/cli.py` — standalone import CLI wrapping `walk()` (Phase 5).
- `feature_skills_webapp/cli_test.py` — CLI ingests dev-store on a fresh DB (Phase 5).
- `feature_skills_webapp/storage/parity.py` — normalised per-logical-key comparison helper (Phase 6).
- `feature_skills_webapp/storage/parity_test.py` — parity helper unit tests (Phase 6).

### feature-skills-webapp — modified

- `feature_skills_webapp/storage/walker.py` — drop the `_apply_tracker_rows` call (Phase 4); whole file deleted (Phase 8) after `walk()` is lifted to the CLI.
- `feature_skills_webapp/storage/walker_test.py` — keystone anti-clobber test (Phase 4); prune walker-only tests (Phase 8).
- `feature_skills_webapp/web/project_page.py` — confirm it reads the features table (the project page *is* the tracker view; the opaque tracker document is rendered separately by `doc_view.doc_shell`); no change expected (Phase 4).
- `feature_skills_webapp/web/doc_view.py` — retire the opaque `features.html` document render + `doc_raw` (Phase 8).
- `feature_skills_webapp/web/discovery.py`, `web/app.py` lifespan — delete watcher/startup walk (Phase 8).
- `feature_skills_webapp/web/synthesis.py`, `web/comments.py`, `web/routes.py` — drop path-keyed `/synthesis-response`, `/comments`, `/comments/integrate`, `/admin/discover` (Phase 8).
- `pyproject.toml` — add the `feature-skills-import` script (Phase 5); drop `watchfiles` (Phase 8).

### feature-skills — modified (SKILL.md flows + export)

- `feature-context/SKILL.md` — author context via PUT; capture the tracker row (Phases 1, 3).
- `feature-requirements/SKILL.md`, `feature-plan/SKILL.md` — author + feedback via PUT, synthesis/comments by key; claim in requirements (Phases 1, 2, 3).
- `feature-implement/SKILL.md`, `feature-iterate/SKILL.md` — plan/requirements updates via PUT; reads by key (Phases 1, 2).
- `feature-review/SKILL.md`, `feature-retro/SKILL.md` — sibling-doc/synthesis reads by key; ship in review (Phases 2, 3).
- `feature/SKILL.md` (router) — resolve state via the listing API + document GET, not file existence (Phase 2).
- `docs/webapp-polling.md` — rewrite for keyed polling (Phase 2).
- `bin/feature-html-to-md` — DB-sourced export; merge mode for `features.md` (Phase 7).

## Phase 1 — Cut document authoring to the API

### What's built

Rewrite the authoring skills so finishing a doc `PUT`s manifest-keyed sections by logical key instead of writing dev-store HTML. Covers context, requirements, plan, and the plan/requirements updates in implement and iterate (writes only; reads stay file-based until Phase 2). Each skill first `GET`s its manifest, maps its content to those section keys, then PUTs; feedback docs PUT an opaque `body`. Drop the skill-side `/admin/discover` force-walk. Cut one skill at a time.

### Key details

- Section keys must match the manifest exactly — fetch it; don't hardcode. Mismatched keys are rejected, which is the point (ends template-vs-manifest drift).
- Use `?dry_run=true` before the real PUT where a skill wants to validate.
- The dev-store write is removed from these flows; the walker still runs (harmless — nothing new to import for these doc types).

### Tests

No automated suite in the skills repo. Empirical: run a feature through context → requirements → plan via the rewritten skills; confirm each doc appears at `/doc/{id}` with no file written under `~/.claude/feature-docs/…` and no manual discover.

### MR chain

One MR in feature-skills titled `feat(skills-api-cutover): author documents via the API`.

## Phase 2 — Migrate reads to the API

### What's built

Move every dev-store read onto the API: interactive reads (comments, synthesis) onto `GET /api/documents/.../{comments,synthesis}` and `POST /api/documents/.../comments/integrate`; sibling-doc reads (review/iterate/retro reading prior plan/requirements/feedback) onto `GET /api/documents/...`. Rewrite `docs/webapp-polling.md` for keyed polling. Re-point the `feature/` router's state detection at the listing API plus a document GET.

### Key details

- Router phase detection currently greps `plan.html` for unchecked `data-checklist-item`s. Replace with: list the feature's documents (`GET /api/projects/{project}/features/{feature}/documents`) to learn a plan exists, then `GET` the plan doc and inspect its `checklist` section content for unchecked items.
- Feedback instance numbering (`-feedback-N`) — derive from document GETs (probe instances) rather than counting `.feedback-archive/` files; the count must include archived instances, which the active-only listing omits.

### Tests

Empirical: run a review/iterate/retro pass that consumes prior docs entirely via `GET /api/documents/...` with the dev-store files moved aside; the router routes a feature correctly with no file stat.

### MR chain

One MR in feature-skills titled `feat(skills-api-cutover): read documents via the API`.

## Phase 3 — Migrate the skills' tracker edits to the mutation API

### What's built

Cut the three tracker-writing skills off hand-editing `features.html`: `feature-context` → `capture` (+ the separate context PUT from Phase 1), `feature-requirements` → `claim`, `feature-review` → `ship`. Handle capture's 409 (existing slug) as already-captured.

### Key details

- Exactly three skills write the tracker: context (capture), requirements (claim), review (ship). **`feature-plan` does NOT claim or write the tracker** — it only reads. (The handoff prose contradicts itself here; the code confirms plan is read-only.)
- The parse still runs this phase, so a walk in this window will *revert* these mutations — durability arrives at Phase 4. Do not assert durable table state here.
- `claim` requires a non-empty `owner`; source it as the skills do today (ask the user / known name).
- The mutation events carry `document_id = NULL`, so observe them via the events feed / project page (the inbox), not by a document lookup.

### Tests

Empirical: capture, claim, and ship a feature through the API; assert each call succeeds and emits its event (`feature_captured` / `feature_claimed` / `shipped`) and the skills no longer touch `features.html`. Durable survival across a walk is the Phase 4 keystone test, not this phase's.

### MR chain

One MR in feature-skills titled `feat(skills-api-cutover): mutate the tracker via the API`.

## Phase 4 — Flip tracker authority to the table

### What's built

Once the stranding gate (decision 3) confirms no skill writes `features.html` and the three writers are on the API, retire `_apply_tracker_rows` so a walk no longer re-derives the `features` table from the file. Confirm the project page already reads the table (no change expected). The opaque `features.html` document render is left in place until Phase 8.

### Key details

- Remove only the tracker-parse application; the rest of `walk()` (document import) is untouched until Phase 5/8.
- If any walker test asserted tracker rows being derived from the file, re-home or delete it to reflect the table now being authoritative.

### Tests

Keystone (new, in `walker_test.py` or `tracker_test.py`): capture or claim a feature via the tracker API, run a `walk()` over a dev-store still containing the old `features.html`, assert the row's status is unchanged by the walk. This test must fail before the flip and pass after. Plus the full suite stays green.

### MR chain

One MR in feature-skills-webapp titled `feat(skills-api-cutover): make the features table authoritative`.

## Phase 5 — Extract a standalone import CLI

### What's built

A new `feature_skills_webapp/cli.py` (decision 4) wrapping the existing `walk()` behind an argparse entrypoint, registered as `feature-skills-import` in `pyproject.toml`. Reuses the existing DB open/migration path from `storage/db.py`. No behaviour change to the embedded walk.

### Tests

- `cli_test.py`: run the CLI against a fixture dev-store on a fresh temp DB; assert the resulting documents/versions match what the embedded `walk()` produces (reuse walker-test fixtures).
- Argument handling: missing `--db`/`--docs-root` errors cleanly; `--reconcile` toggles the reconcile pass.

### MR chain

One MR in feature-skills-webapp titled `feat(skills-api-cutover): standalone import CLI`.

## Phase 6 — Parity check + final import

### What's built

A `storage/parity.py` helper (decision 5) that, given two DBs (or the live DB vs a fresh import), compares normalised section text per logical key and reports mismatches, ignoring the by-construction fields. Then the operational gate: redeploy the service (`uv tool install --editable . --reinstall && systemctl --user restart feature-skills-webapp`), run the final reconciling import via the Phase 5 CLI, and confirm the pass conditions.

### Tests

- `parity_test.py`: identical content across paths → no mismatch; a deliberately differing section → reported; by-construction field differences (source_path/actor/metadata size) → ignored.
- Dogfood (empirical, the gate): re-author this requirements doc via the API and diff clean; final import reports `created=0, updated=0`; zero docs render in raw-fallback.

### MR chain

One MR in feature-skills-webapp titled `feat(skills-api-cutover): parity tooling + final import`.

## Phase 7 — Repoint exports to the DB (merge, not render)

### What's built

Make `bin/feature-html-to-md` (or its replacement) source from the DB instead of dev-store HTML. Per-feature docs render straight; `features.md` merges (decision 6): rewrite only the table-modelled rows in place, preserving row order and the prose "Suggested order" section.

### Key details

- Source content from the DB via the document/listing API rather than reading `~/.claude/feature-docs/…`.
- The merge identifies the table region(s) in the existing `features.md` and replaces only those rows; everything else (ordering, prose) is passed through unchanged.

### Tests

Empirical: with `.feature-workflow.toml` opted in, export a feature's docs and `features.md` from DB content; diff against the previous file-sourced output — per-feature docs identical, and `features.md` ordering + "Suggested order" prose unchanged.

### MR chain

One MR in feature-skills titled `feat(skills-api-cutover): export from the DB (merge tracker)`.

## Phase 8 — Snapshot, then retire the walker, dev-store, and legacy endpoints

### What's built

The irreversible cleanup, behind the Phase 6 parity gate. Archive the dev-store to a dated `tar.gz` left on disk, then remove the dev-store directory and delete: the walker (`storage/walker.py`), the watcher/worker/startup walk (`web/discovery.py` + `web/app.py` lifespan wiring), the legacy path-keyed endpoints (`/synthesis-response?path=`, `/comments?path=`, path-keyed `/comments/integrate`, `/admin/discover`, `/doc/{id}/raw`), the opaque `features.html` tracker document render, and residual `source_path`/`content_html` consumers. Drop the `watchfiles` dependency.

### Key details

- Order: snapshot first, then delete code, then remove the directory.
- Dropping `watchfiles` changes `pyproject.toml` — needs `uv tool install --reinstall` + restart per the project CLAUDE.md, or the service crash-loops.
- Re-home or delete walker/discovery tests; keep the import CLI (Phase 5) which now owns ingestion.
- Dropping the `content_html`/`source_path` columns is a SQLite table-rebuild migration (SQLite can't `DROP COLUMN` cleanly across all versions in use), not a no-op — budget a migration. Alternatively leave the columns unused and only remove the code that reads them; decide at implementation time.

### Tests

Full suite green after deletions. The webapp boots with no walker and no startup walk; a smoke check confirms reads/writes go through the API and the removed endpoints 404.

### MR chain

One MR in feature-skills-webapp titled `feat(skills-api-cutover): retire the walker and dev-store`.

## Verification

Webapp phases (4, 5, 6, 8) — run in `feature-skills-webapp`; all must pass (per CLAUDE.md QC):

- `uv run ruff format --check .`
- `uv run ruff check .`
- `uv run ty check .`
- `uv run pytest` — the full suite (testpaths cover the whole package; expect the suite to grow with the keystone, CLI, and parity tests, and to shrink in Phase 8 as walker tests are removed).

Phase 4 keystone (must fail before the flip, pass after): `uv run pytest -k "keystone or anti_clobber"`.

Phase 5 import CLI end-to-end: `feature-skills-import --db /tmp/parity.db --docs-root ~/.claude/feature-docs --reconcile` (prints a WalkSummary; fresh DB gets the same doc/version counts as the live one).

Phase 6 gate (against the **redeployed** service): final import reports `created=0, updated=0`; `parity` helper reports zero mismatches; this requirements doc re-authored via the API diffs clean; zero docs render in raw-fallback.

Skills phases (1, 2, 3, 7) — no automated suite; empirical acceptance is the per-phase check above (run the rewritten flow against the local webapp at `http://127.0.0.1:8800` and confirm no dev-store file is written/read and no walk is awaited).

## QC

Follow the QA/quality-control steps in each repo's `CLAUDE.md` at implementation time. For feature-skills-webapp that is, before every commit: `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, `uv run pytest` — all green. After dependency or code changes to the deployed service, reinstall/restart per CLAUDE.md so the running service reflects the change.

## Checklist

### Phase 1: Authoring writes (feature-skills)

- Rewrite feature-context to PUT the context document (manifest-keyed) instead of writing dev-store HTML.
- Rewrite feature-requirements to GET its manifest and PUT requirements + feedback docs (opaque body for feedback).
- Rewrite feature-plan to PUT the plan doc by logical key.
- Rewrite the implement/iterate plan & requirements updates to PUT.
- Remove the skill-side /admin/discover force-walk from all authoring flows.
- Empirical: run context→requirements→plan via the API; docs appear at /doc/{id} with no file written, no walk awaited.
- Open one MR in feature-skills: author documents via the API.

### Phase 2: Reads migration (feature-skills)

- Move comments/synthesis reads + comments-integrate onto the keyed /api/documents endpoints.
- Move sibling-doc reads (review/iterate/retro reading prior plan/requirements/feedback) onto GET /api/documents.
- Re-point the feature/ router state detection at the listing API + a plan-doc GET (checklist state from the plan's section content).
- Derive feedback instance numbering from document GETs (covering archived instances), not file counts.
- Rewrite docs/webapp-polling.md for keyed polling.
- Empirical: review/iterate/retro pass consumes prior docs via the API with dev-store files moved aside; router routes correctly with no file stat.
- Open one MR in feature-skills: read documents via the API.

### Phase 3: Tracker skill migration (feature-skills)

- feature-context: call capture (handle 409 as already-captured) instead of editing features.html; keep the separate context PUT.
- feature-requirements: call claim (non-empty owner) instead of editing features.html.
- feature-review: call ship (optional outcome) instead of editing features.html.
- Empirical: capture/claim/ship via the API succeed and emit events; no skill touches features.html (do not assert durable table state — reverted by walk until Phase 4).
- Open one MR in feature-skills: mutate the tracker via the API.

### Phase 4: Authority flip (feature-skills-webapp)

- Run the stranding gate: grep the skills repo for features.html writes returns zero AND the three writers are confirmed on the API.
- Add the keystone anti-clobber test; confirm it fails against current main (parse still applied).
- Remove the _apply_tracker_rows call from walk(); keep document import intact.
- Confirm the project page reads the table (no change needed); re-home/remove any test asserting file-derived tracker rows.
- Keystone test now passes; full suite green (ruff/ty/pytest).
- Open one MR in feature-skills-webapp: make the features table authoritative.

### Phase 5: Import CLI (feature-skills-webapp)

- Add feature_skills_webapp/cli.py wrapping walk() with argparse (--db, --docs-root, --reconcile), reusing the storage db open/migration path.
- Register feature-skills-import in pyproject [project.scripts].
- Add cli_test.py: CLI on a fresh DB matches embedded walk() output; arg errors handled.
- Full suite green; CLI runs end-to-end against the dev-store.
- Open one MR in feature-skills-webapp: standalone import CLI.

### Phase 6: Parity + final import (feature-skills-webapp)

- Add storage/parity.py: normalised per-logical-key section comparison, excluding by-construction fields (reuse doc_diff text extraction).
- Add parity_test.py (match / mismatch / ignored-field cases).
- Redeploy the service (reinstall + restart) before running the gate.
- Run the final reconciling import via the CLI; confirm created=0, updated=0.
- Gate: parity reports zero mismatches; this requirements doc re-authored via API diffs clean; zero docs render in raw-fallback.
- Open one MR in feature-skills-webapp: parity tooling + final import.

### Phase 7: Exports merge (feature-skills)

- Repoint bin/feature-html-to-md to source content from the DB instead of dev-store HTML.
- Implement features.md merge: rewrite only table rows, preserve row order + "Suggested order" prose.
- Empirical: export a feature's docs + features.md from the DB; per-feature docs identical, tracker ordering + prose unchanged.
- Open one MR in feature-skills: export from the DB (merge tracker).

### Phase 8: Snapshot + retire (feature-skills-webapp)

- Archive the dev-store to a dated tar.gz left on disk (outside the walked tree).
- Delete the walker, watcher/worker/startup walk, and lifespan walk wiring.
- Drop the legacy path-keyed endpoints (/synthesis-response, /comments, /comments/integrate, /admin/discover, /doc/{id}/raw) and the opaque features.html document render.
- Remove residual source_path/content_html consumers; drop the watchfiles dependency; reinstall + restart.
- Remove the dev-store directory; re-home/delete walker & discovery tests (keep the import CLI).
- Full suite green; webapp boots with no walker/walk; removed endpoints 404.
- Open one MR in feature-skills-webapp: retire the walker and dev-store.
