# versioned-content-store

## Problem

The webapp can tell you a doc *changed*, but never *what* changed or *why*. During a review round an agent re-saves `requirements.html` or `plan.html`; the walker notices the mtime/size delta (`storage/walker.py` gates re-reads on `source_mtime` + cached `size`) and emits a bare `updated` event; the doc bubbles back into "New since last visit" with no indication of what moved. You click in, re-skim the whole thing, and think "haven't we done this already?" — the recurring annoyance that kicked this off.

The fix we want downstream is a flagged inbox plus a toggleable diff view, so a re-surfaced doc tells you the reason and lets you read just the delta. That's impossible today: **there is no content history to diff against**. The `documents` table stores only metadata — title, size, mtime, `source_path` — and docs are served live from disk by iframing `/doc/{id}/raw`. The `content_html` column laid down in migration 0002 as a "Stage 2 seam" has never been populated.

Two structural weaknesses compound this and matter for what comes next:

- **Change detection is coarse.** It keys on filesystem mtime+size, not content. A no-op re-save (or a re-walk after an unrelated touch) still produces an `updated` event and inbox noise — a version that adds nothing, which is exactly the "haven't we done this already?" pain relocated.
- **Document identity is its file path.** The unique index is on `source_path` — the deepest `~/.claude`-coupling in the schema. Identity-is-path is what makes any non-file writer (a future Codex/API submitter) impossible, and it ties change detection to the file watcher, which has been observed to miss new docs until a manual re-walk.

This feature is the foundation (F1) that makes the downstream work possible. It is deliberately additive: it lands the content model, versioning, and a re-runnable importer, and leaves rendering and interaction completely untouched. F2 (`server-rendered-docs`) is the first consumer that switches rendering to the DB; the flagged-inbox + diff view is a separate later feature needing only F1 + F2. The shape mirrors how `read-state` shipped: plumbing proven before any consumer surfaces it.

## Vision

Every document's content lives in the DB as ordered, structured sections with version history, keyed by a stable logical identity rather than a file path — so a change is "which sections differ", a new version is cut only when content actually changed, and re-running the importer is a safe no-op.

## Non-goals

F1 is foundation. To keep the additive promise honest, it explicitly does **not**:

- **Render from the DB or drop the iframe.** Rendering stays file-from-disk; that's F2's job.
- **Change the inbox query or any user-facing surface.** The one user-visible effect is that no-op re-saves stop producing inbox noise — a consequence of content-based change detection, not a new surface.
- **Add a submission API or change feature-skills.** Agents keep writing files; the importer ingests them. The cross-agent write contract is a later feature (`agent-submission-api`).
- **Section-parse feedback or tracker docs.** Those ride the versioned model as opaque whole-documents (see Data model); only context / requirements / plan are decomposed into sections in F1.
- **Back-fill real version history.** On-disk files carry none; import seeds a single current version and history accrues from then on.
- **Fix the missed-walk / file-watcher fragility.** F1 makes change detection content-based *once a walk runs*; it doesn't change *when* walks run. That fragility is structurally removed only when an explicit-write API lands (a later feature).

## User stories

1. As Nigel, running the webapp day to day
  I want a no-op re-save of a doc to produce no new version and no
        inbox entry
  An agent re-writes

  with the
        same content (a reformat, a re-run of the workflow, an unrelated

  ).
        Today it reappears in "New since last visit". After F1, content-based change
        detection sees nothing changed and the inbox stays quiet — the inbox stops crying
        wolf. This is the one user-visible win F1 delivers on its own.
2. As the F2 (server-rendered-docs) implementer
  I want each doc's content available as ordered, structured
        sections in the DB, through a defined access seam
  F2 renders docs server-side and drops the iframe. It reads the
        stored sections for a doc (context / requirements / plan) and lays them out itself,
        instead of iframing the file from disk. F1 must have populated that content for the
        whole corpus, and left a clearly-defined way to fetch it, before F2 can start.
3. As the future diff/flagged-inbox feature
  I want two stored versions of a doc I can compare section by
        section
  A re-surfaced doc needs to show "sections X and Y changed".
        That's a comparison of the current snapshot against its predecessor — which only
        exists if F1 has been accruing per-version snapshots since import.
4. As the future cross-agent submission API (and a Codex writer)
  I want documents addressed by a stable logical identity, not an
        absolute file path
  A non-file agent submits a structured update for "the
        requirements doc of feature X in project Y". For its write to land on the same row a
        file import would, identity must be decoupled from

  — a
        decision F1 makes cheaply and additively now, even though F1 itself only writes via
        the importer (stamping a constant

  actor the API later replaces
        with a real agent identity).
5. As Nigel, performing the eventual cutover
  I want the importer to be re-runnable and idempotent across the
        whole existing dev-store
  The planned cutover is "run the importer one last time, flip
        the skills over to submitting directly, done". That only works if a full re-import
        reconciles cleanly — cutting versions only where content genuinely changed, never
        duplicating or corrupting rows, and never silently dropping a doc.

## Data model

The model is deliberately **generic**, because the golden-path section layout is stable today but will be trimmed, reordered, and extended over time. Adding, removing, or reordering sections must be a *content* change, never a schema migration.

### Structured content and the section manifest

A document's content is `{doc_type, ordered sections}`, where each section is `{key, body}`:

- **Only the skeleton is structured** — which sections exist and in what order. Section *bodies are opaque, trusted HTML fragments*, stored whole. This preserves a section's freedom to embed a diagram or arbitrary markup and keeps us out of modelling prose. The trust model is unchanged: content comes from our own agents, not untrusted users, so this is not a sanitisation question.
- A **per-doc-type section manifest** describes the expected layout for each type and is the source of truth for "what sections should this doc have" — not the schema. The manifest must express three shapes, because the five doc types do not share one uniformly:
    - **Ordered sections** — context, requirements. A fixed, ordered set of keys, enumerating *all* template sections including optional ones (e.g. requirements' `design-notes`).
    - **Repeated / optional sections** — plan. Carries one-or-more `phase-N` sections generated per plan, plus optional `qc`/`checklist`; the manifest expresses cardinality, not just a flat list.
    - **Opaque whole-document** — feedback and the tracker. Stored and versioned as a single opaque body, not decomposed into sections (see below).

### Feedback and tracker are opaque, dual-represented docs

Feedback docs are interactive synthesis *forms*, not authored prose: they have no `<main class="document">`, no `<section id>`, and no `feature-doc-type` meta tag (they're filename-typed). Their *structured* content already has a home — the existing `synthesis_responses` table. So feedback rides the versioned model as a **single opaque body** (for change-detection and history) while its structured responses keep flowing through `synthesis_responses`. This mirrors the tracker: `features.html` is ingested and versioned as an opaque body *and* its rows are extracted into the `features` table for the inbox's in-progress / shipped queries. Both are dual-represented; only context / requirements / plan are section-parsed.

### Versions

- A document has **many versions**; each version is a content **snapshot** of the ordered sections (or the opaque body) at a point in time. Snapshot-per-version is the chosen shape (not sections as first-class per-version rows): it keeps diffing simple — compare two snapshots, scope per section — and nothing downstream needs per-section version identity.
- A new version is cut **only on real content change**. The governing invariant: a no-op re-scan or resubmission cuts *zero* versions and emits no event; a genuine authored edit cuts *exactly one* version and emits the existing `updated` event the inbox already consumes. Content equality simply replaces mtime as the gate on that event.
- Each version records the **originating actor** — a constant `importer` in F1, a placeholder the future API populates with a real agent identity — and a timestamp. **The "change reason" (which sections differ) is derived on read** by diffing a snapshot against its predecessor; it is not a stored field (storing it would duplicate state that can drift, and nothing in F1 consumes it).
- **Import seeds a single current version**; history accrues from then on (see Non-goals — no back-fill).
- **Versions track content, not status.** A version is cut only on a content change while a doc is `active` or `archived`. Archival and going `missing` are status transitions, not content changes, so they cut no version; a doc returning from `missing` with changed content cuts a version on reactivation.

### Logical identity

- Documents gain a **stable logical identity** decoupled from `source_path`. The importer derives it from the path; a future API supplies it directly — *the same key both ways*, so file-import and API-submit converge on the same row and the eventual cutover stays clean.
- `source_path` becomes a derived/optional input, and the uniqueness constraint moves from `source_path` to the logical key.
- Feedback docs break the one-doc-per-type assumption — they are `<phase>-feedback-<N>`, multiple per phase — so the identity needs an **instance discriminator**. Worked example: a feedback doc's logical key is effectively `(project, feature, phase, "feedback", N)` (e.g. `(kea, synthesis-verify-retry-v2, requirements, feedback, 1)`), with active-vs-archived tracked as a *separate* status dimension (it already is, via the walker's `archived` flag). Both the importer and a future API must derive that identical tuple.
- **Existing rows migrate by backfill, not drop-and-re-import.** The migration computes the logical key for every existing row from its stored `source_path` (the same derivation the importer uses), then moves the unique index — without changing `documents.id`. This preserves the rows that foreign-key into documents: `read_state` (what you've read), `synthesis_responses` (answers you've submitted), and `comments`. Drop-and-re-import would cascade-delete all of that; backfill keeps it.

### The F2 content-access seam

F1 must leave a **clearly-defined content-access seam** for F2 to render from — a version/content accessor and/or the existing `content_html` column. Whether the snapshot supersedes `content_html` or sits beside it is a plan decision, but the obligation to leave a defined seam is a requirement, because F2's rendering switch depends on it.

## Technical approach

**Evolve the walker into the importer — don't build a parallel system.** `storage/walker.py` already scans the dev-store, parses the `feature-doc-type` meta tag and title, parses the tracker rows, and gates re-reads on mtime+size. F1 grows that same code to parse section structure, store versioned content, and detect change by content. It is the natural place this logic lives.

**Separate authored content from template chrome reliably across the whole corpus.** For the section-parsed types (context, requirements, plan), the importer must extract the authored sections and nothing else — no comment-rail or popover chrome. This is the feature's **main risk**, so it is de-risked first, by proving the parser against every existing doc of those types before any schema lands. (The mechanics of how the parse isolates authored content are in the Indicative notes.)

**Content-based change detection is the versioning trigger.** On each scan the importer parses the content, compares it against the stored current version, and cuts a new version only on a real difference — satisfying the no-op-cuts-nothing / genuine-edit-cuts-exactly-one invariant above. The mtime+size gate may remain as a cheap "should I bother re-reading this file" pre-filter, but it no longer decides whether something changed.

**Un-ingested docs must be observable, never silently skipped.** A doc that can't be ingested as its type expects must surface (a `WalkSummary` count and/or an event), and must never abort the whole walk (mirroring today's guarded tracker parse). Silent skip is ruled out — F1's value (content is in the DB) quietly not applying to a doc is a bad outcome that should be visible.

**Additive, coexisting with the current render path.** F1 does not touch rendering: `/doc/{id}/raw` still iframes the file from disk, so the existing `source_path`/metadata write must keep working alongside the new content/version write. F1 adds the versioning pass over the same scan rather than ripping out the metadata path — that path is F2's to retire.

Testing **inherits the existing storage conventions** (no-network, per-worker DB, the `transaction()` helper); the corpus of real dev-store docs is the parser's acceptance test.

## Alternatives considered

1. A1 — Raw rendered HTML in the DB
  Source: design doc / captured context
  Store each doc's whole HTML blob, versioned. Gives diffs and a
        reason channel, but keeps the template-drift fragility: interaction state still has to
        be scraped back out of a rigid rendered document, which is exactly what F2 is meant to
        retire. Rejected — solves history but not the brittleness.
2. B — Git-backed dev-store
  Source: design doc / captured context
  Put the dev-store under version control and diff commits. Keeps
        the coarse mtime walker, gives no explicit "why did it change" reason channel, and does
        nothing for the path-coupled identity problem. Rejected — doesn't address the core
        pains.
3. A2 — Structured content in the DB, rendered server-side (chosen)
  Source: design doc / captured context
  The only option that solves all three problems at once: free
        diffs, an explicit reason channel, and a path to retiring the iframe-DOM-scraping
        fragility. F1 lands the model and importer; F2 consumes it.
4. Section-parse feedback docs (rejected); chose opaque + synthesis_responses
  Source: review round 1, confirmed with user
  ~41% of the corpus (83 of 202 docs) are feedback docs with no

  /

  structure and no meta tag —
        they're interactive forms, not prose. Trying to section-parse them is unworkable and
        pointless: their structured content already lives in

  .
        Chosen instead: feedback rides as a single opaque versioned body, mirroring the
        tracker's dual representation.
5. Per-section version rows vs. snapshot-per-version (chose snapshot)
  Source: captured context, confirmed review round 1
  Modelling each section as a first-class versioned row was
        considered and rejected: snapshot-per-version keeps diffing simple (compare two
        snapshots) and nothing downstream needs per-section version identity. Decided, not open.

## Delivery phases

Three ordered increments, each one MR, following the project's established rhythm (`read-state`, `doc-discovery`). The riskiest unknown — the section parser — is proven first, before any schema lands.

### Phase 1 — Section parser + per-type manifests

A pure parser that turns a doc's HTML into an ordered list of `(section-key, body)` pairs, plus a manifest per doc type expressing the three shapes (ordered / repeated-optional / opaque). No schema, no DB writes. **Scope:** section-parse context / requirements / plan; feedback and the tracker are recognised and handled as opaque whole-bodies, not decomposed. **Testable value:** run it across the entire existing dev-store corpus and confirm every context / requirements / plan doc parses into its expected sections (incl. plan's repeated `phase-N`), and that feedback/tracker docs are correctly classified as opaque. De-risks the feature's biggest unknown before anything depends on it.

### Phase 2 — Versioned content model, importer + logical identity

The migration that adds content/version storage and the logical-identity key, moving the unique index off `source_path` by **backfilling** the logical key for existing rows (preserving `read_state` / `synthesis_responses` / `comments` — no id churn). The walker grows into the importer: it parses content (Phase 1), compares against the stored current version, and cuts a new version only on real change, stamping the `importer` actor and leaving a defined content-access seam for F2. Un-ingested docs surface in `WalkSummary`. **Testable value:** re-running over unchanged files yields zero versions and zero events; a genuine edit yields exactly one new version; existing read-state and submitted responses survive the migration. Rendering still serves from disk — additive and unsurfaced.

### Phase 3 — Tracker ingestion + full-corpus cutover proof

Bring `features.html` into the same versioned model (opaque body) while keeping its row extraction into the `features` table intact, and prove the importer is idempotent across the whole existing corpus — feedback and archived docs included — in one reconcile pass. **Testable value:** a full re-import over the real dev-store is a clean no-op on a second run; mutating one section of one doc yields exactly one new version on that doc and zero elsewhere (single-change isolation); the tracker is both versioned and row-extracted; the inbox queries still work unchanged.

## Indicative implementation notes

Plan-level detail and still-open decisions, carried forward for `/feature-plan`. Not requirements — the plan settles these.

- **Home of the code.** Grow `storage/walker.py` in place (it already holds `_MetaParser`, `_TrackerParser`, `parse_tracker`, the mtime gate, and the event/upsert logic); don't fork a parallel module.
- **Parse target (mechanics).** For section-parsed types: ordered `<section id>` inside `<main class="document">`. The `<header class="doc-header">` is `main`'s first child and not a section; the comment chrome (`#comment-trigger`, `#comment-popover`, `aside.comments-rail`, sticky footer) are siblings of `main` and excluded for free. Feedback docs have none of this structure → opaque.
- **Manifest completeness.** A manifest must enumerate all template sections including optional ones (e.g. requirements' `design-notes`), so the parser knows the full expected set rather than assuming the golden path.
- **The `content_html` seam.** Migration 0002 left an unused `content_html` column as the Stage-2 seam. Decide whether the version snapshot supersedes/replaces it or sits beside it — F1's obligation is only to leave a defined access seam (a version accessor and/or `content_html`).
- **Content-equality definition (open — means only).** The invariant (no-op → 0 versions, genuine edit → exactly 1) is fixed in the body; the *means* is open: byte-equality of the serialised sections vs. a normalised compare (whitespace, attribute order). Lean conservative-but-normalised; settle in the plan.
- **Logical-identity representation.** A composite of existing columns (`project`, `feature`, `type`, plus the feedback instance discriminator `N`) — *not* a separate `identities` table or a normalised key model, which would over-build F1. A composite unique index over existing columns + discriminator is almost certainly enough. Templates already carry a convention: `const docId = 'docs/features/<feature>/<type>'`.
- **Backfill migration mechanics.** Add the logical-key column, populate it for every existing row from its `source_path` via the same derivation the importer uses, then move the unique index — preserving FKs into `documents.id`. (Drop-and-re-import remains the importer's normal everyday mode; the one-time migration just must not nuke the table.)
- **Un-ingested-doc observability mechanism.** A `WalkSummary` count and/or an event row; exact mechanism is the plan's call. The requirement is only that it's visible.
- **Walk rewiring (open).** Decide whether F1 rewires the existing walk in place or adds versioning as a second pass over the same scan. Both the old metadata path (for the disk-rendered iframe) and the new content/version path must coexist through F1.
- **feature-skills is unchanged in F1.** Webapp-only feature. The skills keep writing files to the dev-store; the importer ingests them. Switching skills to submit directly is a later cutover feature (`agent-submission-api`).

## Design notes

- **Feedback & tracker ride as opaque whole-docs** (round 1). Feedback docs lack the `<main>`/`<section id>` structure and meta tag, and are forms not prose; ~41% of the corpus. They're versioned as a single opaque body, with structured content staying in `synthesis_responses` — mirroring the tracker's dual representation. Only context / requirements / plan are section-parsed.
- **Change reason is derived, not stored** (round 1). "Which sections differ" is computed by diffing adjacent snapshots on read; versions store snapshot + actor + timestamp only. Avoids duplicated state that can drift.
- **Existing-row migration is backfill, not drop-and-re-import** (round 1, user's call). The logical key is computed from each row's stored `source_path` and the unique index swapped in place, preserving `read_state` / `synthesis_responses` / `comments` that FK into `documents.id`.
- **Versions track content, not status** (round 1). Archival / missing are status transitions and cut no version; reactivation-with-change does.
- **F1 inherits the missed-walk fragility by design** (round 1). It makes detection content-based once a walk runs but doesn't change when walks run; the fragility is fixed only when an explicit-write API lands. Captured as a Non-goal so the Problem section doesn't over-promise.
