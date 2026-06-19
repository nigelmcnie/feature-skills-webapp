# skills-api-cutover

## Problem

The structured-content arc is built but only half-adopted. F1 (`versioned-content-store`), F2 (`server-rendered-docs`), and F3 (`agent-submission-api`) have all shipped, so the webapp can store, version, render, and *be written to* entirely through a logical-key HTTP API. The contract for the end-state exists in full. What's missing is that **nothing uses it** — no `feature-*` skill references `/api/documents` at all.

Today the doc-authoring skills — context, requirements, plan, and the plan/requirements updates in implement and iterate, roughly five of the nine `feature-*` skills — write standalone HTML files into `~/.claude/feature-docs/<project>/<feature>/` and rely on the webapp's filesystem walker to import them. A separate set (review, iterate, retro) *reads* those files as inputs. The DB is the source of truth for **reading and rendering**, but the dev-store is still the source of truth for **authoring**. That split keeps three problems alive:

- **Template-vs-manifest drift.** Skills copy section structure from HTML templates; the webapp owns the real manifest. When they diverge, sections silently go missing (the `verification`-section incident in F3 — a plan section that existed in the template but not the manifest, for a long time).
- **File-watcher flakiness.** New docs are invisible until a walk runs; this session alone saw two cases where a freshly written doc needed a manual `POST /admin/discover` before it appeared.
- **Claude-only authoring.** The write path is bound to `~/.claude` paths, HTML templates, and watcher timing — so it can never be shared with Codex.

The capability is all there. This feature is the adoption: move authoring onto the API, verify it produces equivalent state, then delete the file-based path for good.

## Vision

Every feature-* skill authors documents and tracker changes through the logical-key API and fetches its section structure from the manifest — the dev-store, the walker, and the path-keyed endpoints are gone, and the DB is the single source of truth for reading *and* writing.

## User stories

1. As a feature-* skill (the authoring agent)
  I want to write a document by its logical key and fetch its
        section structure from the manifest, instead of emitting a templated HTML file
  The requirements skill finishes a draft. It fetches

  , assembles section bodies under
        those keys, and

  s them to

  . The
        doc appears in the webapp immediately — no file written, no walk awaited, no
        chance of a section key the manifest doesn't know about.
2. As a feature-* skill collecting review feedback
  I want to read comments and synthesis responses by logical
        key, not by absolute filesystem path
  After publishing a feedback doc, the skill polls

  and

  and marks comments integrated via
        the keyed endpoint — never constructing a tilde-expanded

  URL that 404s if the expansion is wrong.
3. As Nigel, running the workflow
  I want the cutover to never strand my in-flight work or
        silently lose a document
  The skill being rewritten is the same machinery that
        captured this very requirements doc. The file-based path keeps working until
        the API path is proven equivalent by a parity check; only then is the
        dev-store deleted.
4. As Nigel, maintaining the webapp
  I want the walker, the watcher, and the legacy path-keyed
        endpoints gone once nothing writes files
  After cutover, the webapp boots without a filesystem
        walk, has no

  task, and serves no

  or

  — there
        is less surface to reason about and no walk-timing race to debug.
5. As a future Codex wrapper (downstream beneficiary)
  I want the authoring contract to depend on nothing in
  Once the Claude skills are thin wrappers over the API,
        standing up

  (a later feature) means
        pointing the same calls at the same endpoints — no dev-store, no HTML
        templates, no Claude-specific assumptions.

## Scope

This feature spans two repos and finishes with an irreversible deletion. What's in and what's explicitly out:

### In scope

- **Authoring writes.** Rewriting the doc-authoring `feature-*` skills (context, requirements, plan, and the plan/requirements updates in implement and iterate) to author via `PUT /api/documents` and fetch `/api/manifests/{doc_type}` instead of writing dev-store HTML.
- **Reads.** Moving the interactive reads (comments, synthesis) *and* the sibling-doc reads onto the API. Several skills read dev-store files as inputs — review and iterate read prior docs/synthesis, and retro reads `plan.html`, `requirements.html`, `.feedback-archive/*.html`, and the tracker. These move to `GET /api/documents/...` (and the tracker read to the tracker API), or Phase deletion would silently break them. The path-keyed polling convention in `docs/webapp-polling.md` is retired.
- **Tracker mutations.** Cutting claim / move / ship onto the typed tracker API delivered by `agent-submission-tracker-ops` (a hard prerequisite — see Technical approach).
- **Standalone import CLI.** Extracting a re-runnable import command from the walker's `walk()` before the walker is deleted, so fresh-DB / cross-repo ingestion survives (and the final reconciling import has a tool). No such CLI exists today — the walker only runs embedded in the server.
- **Exports.** Repointing repo exports so `feature-html-to-md` (or its replacement) regenerates from DB content rather than dev-store HTML; exports stay optional, gated by `.feature-workflow.toml`.
- **Parity gate.** A mechanical parity check that proves the API-authored path produces equivalent DB state to the file+walker path, plus the final reconciling import — run before any deletion.
- **Retirement (irreversible).** Snapshotting the dev-store to a tar.gz kept on disk, then deleting the dev-store and retiring the walker, watcher, startup walk, and the legacy path-keyed endpoints (`/synthesis-response?path=`, `/comments?path=`, the path-keyed `/comments/integrate`, `/admin/discover`, and the `/doc/{id}/raw` raw-fallback render target).

### Out of scope

- **Codex wrappers.** Standing up `~/.codex/skills/feature-*` is a follow-up once the Claude skills are proven on the API. This feature makes that follow-up clean but does not do it. Capture it as a new tracker entry.
- **Building the tracker API.** The typed claim/move/ship surface is `agent-submission-tracker-ops`' job; this feature *consumes* it and depends on it landing first.
- **New document types or manifest changes.** The manifests are fixed by F1–F3; this feature adopts them as-is.

## Technical approach

### Adopt, don't build

The server contract is complete. The work is almost entirely on the **skills side** (the `feature-skills` repo): rewrite each flow from "render template HTML → write file → force a walk → poll by path" to "fetch manifest → assemble sections → `PUT` by logical key → poll by key". The **webapp side** is deletion, not construction.

### Tracker cutover depends on agent-submission-tracker-ops

The walker does double duty: it imports per-feature docs *and* parses `features.html` into the `features` table that the inbox and project pages query. The document API deliberately refuses to write the `features` doc type. So the walker cannot be fully deleted until tracker mutations have somewhere else to go. We will therefore **depend on `agent-submission-tracker-ops` landing first** and cut claim/move/ship onto its typed API — letting the walker be deleted whole rather than left half-alive to keep parsing the tracker. This makes tracker-ops a hard prerequisite alongside F1/F2/F3. The dependency is inherited by everything from the tracker-cutover phase onward (parity must include tracker state; deletion requires tracker mutations to be off the file) — only the authoring-write and read-migration phases can proceed without it.

### Cut over one skill at a time, no in-skill dual-write

Each skill's authoring/read code is swapped in place, so once a skill is on the API there is no per-invocation file fallback. Rather than carry a dual-write path in every skill (which doubles each flow and invites drift), we cut skills over **one at a time, each verified before the next**, with the dev-store kept readable and the walker alive until the final phase. If a freshly-cut skill misbehaves, the fix is to that one skill — not a runtime fallback.

### Cut over, prove parity, then delete

The deletion of the dev-store is the one irreversible step, so it comes last, behind a parity gate. The importer's idempotency (an F1 design property, established precisely so the dev-store could be ingested one last time) makes a final reconciling import safe. Until that gate passes, the file-based path stays functional — important because the skill under rewrite is the same machinery the workflow runs on, so a hard swap could break the authoring loop itself.

The parity gate must be a **mechanical pass/fail, not a prose judgement**, because it guards an irreversible deletion. The stored-content serialiser is byte-sensitive (whitespace, attribute order) and explicitly not semantic, so a template-authored doc and an API-assembled doc will differ in bytes while being semantically identical — parity must compare *normalised section content*, not raw bytes. Some fields also differ *by construction* between the two write paths and are excluded from the comparison: `source_path` (a real path vs NULL), `actor` (`importer` vs `agent`), the `metadata` size key, and event provenance (`created`/`updated` rows). Concrete pass conditions: every logical key authored both ways matches under that normalisation; the final import reports `created=0/updated=0`; and zero documents fall into the raw-fallback render path (see below). The check must run against the **redeployed** service, since the long-running `uv tool` service won't reflect edits until reinstall+restart.

**Recoverable deletion.** Before deleting, the dev-store is archived to a dated `tar.gz` left on disk (outside the walked tree), turning "irreversible" into "recoverable for a retention window". The archive is removed manually once the new path has proven itself.

**The raw-fallback target is not a dead remnant.** The doc shell still falls back to a `raw-fallback` mode that iframes `/doc/{id}/raw` (which reads `source_path` from disk) whenever a document's current content is missing or shape-mismatched. Deleting that endpoint is gated on the parity check proving no document reaches raw-fallback after cutover — it is a real precondition, not trivial cleanup.

### The manifest becomes the single source

Skills fetching `/api/manifests/{doc_type}` at authoring time is what *structurally* ends template-vs-manifest drift: there is no second copy of the section list to drift from. This realises the "webapp owns the manifest" decision from F2.

### Exports regenerate from the DB

Today `feature-html-to-md` reads dev-store HTML. With no dev-store, the export must source its content from the DB instead — either a webapp export endpoint that renders a doc to markdown/HTML, or a CLI that reads the API. Exports remain optional and per-repo; only their *source* changes. (Which mechanism and what triggers it is a plan-level decision — see Indicative notes.) The export repoint comes **after** the parity gate: exports source from the DB, so the DB should be proven equivalent before it's trusted as the export source.

### Keep an import path alive

The walker is the only thing that ingests HTML into the DB, and it only runs embedded in the server (startup walk + watcher + `/admin/discover`); there is no standalone import command. Rather than lose ingestion when the walker is deleted, we extract a re-runnable import CLI from `walk()` first. It serves the final reconciling import and leaves a path to ingest a fresh DB or another repo's `docs/` later (e.g. for the Codex follow-up). This resolves the context doc's open question on whether import survives deletion: it does, as a CLI.

## Alternatives considered

1. Keep tracker file-based; retire only the per-doc import path
  Source: discussed with Nigel (requirements kickoff)
  Would decouple this feature from

  by keeping the walker's

  parsing alive while removing only doc import. Rejected:
        it leaves the walker half-deleted and the file-based tracker path lingering
        indefinitely — Nigel chose the clean end-state where the walker is deleted whole,
        accepting tracker-ops as a prerequisite.
2. Absorb the tracker API into this feature
  Source: discussed with Nigel (requirements kickoff)
  Build the typed claim/move/ship surface here so there's
        no cross-feature dependency. Rejected: it merges two features and inflates scope;

  already exists as a tracked,
        separately-designed slice. Depend on it instead.
3. Build Codex wrappers in this feature too
  Source: discussed with Nigel (requirements kickoff); codex/plan.md step 5
  Land both agents on the shared contract at once. Rejected:
        prove the Claude skills on the API in anger first; Codex wrappers become a thin
        follow-up once the contract is exercised. Lower risk, smaller blast radius.
4. Cut over now, delete the dev-store in a later feature
  Source: discussed with Nigel (requirements kickoff)
  Defer the irreversible deletion until the API path has run
        for real. Rejected: Nigel chose to include the deletion here, gated behind the
        parity check, so the arc actually finishes rather than leaving dead file-based
        infrastructure in place.
5. Delete the import path entirely with the walker
  Source: review round 1 + discussed with Nigel
  Since the walker is the only importer and nothing in scope
        needs fresh-DB ingestion, drop import altogether. Rejected: Nigel chose to build a
        standalone import CLI now — "better to have it and not need it" — so cross-repo /
        fresh-DB ingestion (and the Codex follow-up) isn't blocked later. Small cost now,
        removes a future blocker.

## Delivery phases

Ordered so each phase delivers testable value and the irreversible deletion is last, behind the parity gate. The file-based path stays functional until the final phase. Phases 1–2 can proceed without tracker-ops; everything from Phase 3 onward inherits the tracker-ops dependency.

### Phase 1 — Cut document authoring to the API

Rewrite the authoring skills (context, requirements, plan, and the plan/requirements updates in implement and iterate) to fetch the manifest and `PUT` section bodies by logical key. Drop the skill-side `/admin/discover` force-walk. **Testable:** run a feature through context → requirements → plan via the API; docs appear in the webapp with no file written and no walk awaited.

### Phase 2 — Migrate reads to the API

Move the interactive reads (comments, synthesis) and the sibling-doc reads onto the API: review/iterate read prior docs and synthesis, and retro reads `plan.html`/`requirements.html`/`.feedback-archive` by key instead of by path. Retire the path-keyed polling in `webapp-polling.md`. **Testable:** run a review/iterate/retro pass that consumes prior docs entirely through `GET /api/documents/...`, with the dev-store files moved aside.

### Phase 3 — Cut tracker mutations to the tracker API

Move claim / move / ship onto the typed tracker API from `agent-submission-tracker-ops`, and the tracker *read* too. Skills stop editing `features.html` directly. **Testable:** claim and move a feature; the inbox and project rows update with no file write and no walk. (Depends on tracker-ops being shipped.)

### Phase 4 — Extract a standalone import CLI

Lift the walker's `walk()` into a re-runnable import command that can ingest dev-store / repo HTML into the DB without the embedded server. This is the ingestion path that survives the walker's deletion and the tool the final import uses. **Testable:** run the CLI against the dev-store on a fresh DB and get the same documents/versions the embedded walk produces.

### Phase 5 — Parity check + final import

Prove the API-authored path produces DB state equivalent to the file+walker path under normalised section comparison (excluding the by-construction fields), then run the final reconciling import via the CLI. **Testable (the gate):** every logical key authored both ways matches; the final import reports `created=0/updated=0`; zero documents render in raw-fallback; re-authoring this very requirements doc via the API diffs clean — all against the redeployed service.

### Phase 6 — Repoint exports to the DB

Make the opt-in repo export regenerate markdown/HTML from DB content instead of dev-store HTML. **Testable:** with `.feature-workflow.toml` opted in, export a feature's docs and `features.md` from DB content and diff against the previous file-sourced output.

### Phase 7 — Snapshot, then retire the walker, dev-store, and legacy endpoints (irreversible)

Archive the dev-store to a dated `tar.gz` left on disk. Delete the filesystem walker, the watcher and startup walk, and the legacy path-keyed endpoints (`/synthesis-response?path=`, `/comments?path=`, path-keyed `/comments/integrate`, `/admin/discover`, `/doc/{id}/raw`); remove the dev-store directory; drop the now-unused `watchfiles` dependency (reinstall+restart). **Testable:** the webapp boots with no walker and no walk; all reads and writes go through the API; the full suite is green.

## Indicative implementation notes

Plan-level detail surfaced during requirements work, carried forward for `/feature-plan`. Not requirements; do not treat as binding.

### API surface to consume (already built, F3)

- `PUT /api/documents/{project}/{feature}/{doc_type}/{instance}` — body `{"sections": {key: html}, "actor": "agent"}` for section docs; `{"body": html}` for opaque docs (feedback). Returns `{logical_key, document_id, version_num, url, created, changed}`. Supports `?dry_run=true` → `{"valid": true}`.
- `GET /api/documents/.../{instance}` — returns sections in manifest order.
- `GET /api/manifests/{doc_type}` — returns `{shape, sections:[{key,label}], repeated_prefixes}`. Fetch this to learn section keys before assembling a write.
- `GET /api/documents/.../comments`, `POST /api/documents/.../comments/integrate` (body `{"ids":[...]}`), `GET /api/documents/.../synthesis` — the keyed replacements for the path-based reads.
- `validate_writable` (storage/documents.py) permits only `context`, `requirements`, `plan`, and `*-feedback`; `instance != 1` only for feedback. The `features` type is intentionally not writable here — hence the tracker-ops dependency.

### Webapp deletions (Phase 7)

- `storage/walker.py` (whole file — note Phase 4 first lifts its `walk()` into a standalone import CLI), `web/discovery.py` (`_watch`, `_worker`, `request_walk`), and the lifespan walk wiring in `web/app.py`.
- Legacy endpoints: `web/synthesis.py` path handler, `web/comments.py` path handlers, `web/routes.py` `admin_discover`, `web/doc_view.py` `doc_raw` (gated on the Phase 5 "no raw-fallback" check).
- Residual consumers of `source_path` (uniformly NULL post-cutover) and the legacy `content_html` column — clean these up too.
- Drop the `watchfiles` dependency (only consumer is `discovery.py`); per the project CLAUDE.md this needs `uv tool install --reinstall` + restart.
- Watch out for tests and fixtures that exercise the walker; some may need re-homing onto the API submission path (or the new import CLI) rather than deletion.

### Skills to rewrite (feature-skills repo)

- The 8 doc-writing `feature-*/SKILL.md` flows, plus `docs/webapp-polling.md` (rewrite for keyed polling) and the `bin/feature-html-to-md` export path (DB-sourced).
- The feature router (`feature/`) and any sub-skill that resolves state from dev-store file existence will need to resolve from the API instead.

### Parity check shape (Phase 5)

- F3 reused the walker's `logical_key` + version-on-change, so both write paths converge on the same rows by design — the check is largely structural confirmation. A dual-run that authors a representative feature via both paths and diffs the resulting documents/versions/sections (and comments/synthesis) is the likely shape.
- Compare *normalised* section content (e.g. extracted section text), not raw `serialise()` bytes, which are whitespace/attribute-order sensitive. Exclude the by-construction differences: `source_path`, `actor`, `metadata` size, event provenance.
- Free dogfood case: re-author *this* requirements doc via the API and diff — its section ids are all valid `requirements` manifest keys, so it round-trips cleanly.
- Run the check against the redeployed service (the long-running `uv tool` service won't reflect edits until reinstall+restart).

### Skill mechanics post-cutover

- The rewritten skills must compute the feedback instance number (`-feedback-N`) by counting via `GET`, not by counting `.feedback-archive/` files, which won't exist.

## Design notes

- **Requirements kickoff.** Three scope decisions taken with Nigel before drafting: (1) tracker mutations cut to the API — depend on `agent-submission-tracker-ops` first so the walker is deleted whole; (2) Codex wrappers are a follow-up, not in scope; (3) dev-store + walker deletion is in this feature, as the final phase behind a parity gate.
- **Round 1 (review synthesis).** Build the standalone import CLI now rather than defer it — "better to have it and not need it" — so cross-repo / fresh-DB ingestion isn't blocked later (reverses the drafted recommendation to defer). Snapshot the dev-store to a `tar.gz` left on disk before deletion, making the irreversible step recoverable.
- **Round 1 (review synthesis).** Corrections folded in: retro is a read-path migration (not an authoring rewrite) and would otherwise break on deletion; the authoring surface is ~5 skills, not 8/9; the parity gate is mechanical (normalised comparison, excluded-by-construction fields) since it guards an irreversible deletion; `/doc/{id}/raw` is the live raw-fallback target, deletion gated on a "no raw-fallback" check; parity precedes the export repoint; Phase 1 split into authoring-writes and read-migration; the tracker-ops dependency is inherited from the tracker phase onward.
