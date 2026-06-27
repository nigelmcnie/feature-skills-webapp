# api-coherence — Requirements

## Summary

This webapp gives agents an HTTP surface to drive the feature workflow — creating documents (context, requirements, plan), managing the feature tracker (capture / claim / ship / …), and exporting a `features.md` snapshot back into the repo. It works, but it has accreted across roughly six shipped features and the pieces no longer agree. Before we wrap a typed MCP facade around it (so other agents like Codex can call these as tools), we tidy the surface into one coherent resource model — otherwise the facade would harden the rough edges in place.

Two concrete bugs, both hit while capturing the MCP-facade feature itself, show what is wrong:

- **Notes get silently dropped.** Writing a document quietly creates the tracker row for that feature with *empty* notes. The explicit “register with notes” call then errors because the row already exists, and the notes are thrown away. A prior feature, `tracker-feature-notes-update`, already shipped a *recovery* for this (a dedicated, idempotent note-edit operation) and *planned a prevention* — reordering the context skill to register the feature before writing any document — but that reorder was never delivered, so the bug is still live.
- **The export disagrees with the database.** The `features.md` export merges database state into the *existing file* rather than rendering from canonical state, so a value set only in the DB never reaches the repo snapshot — the two silently diverge.

Both are symptoms of one root cause: feature existence has no single front door (it can be conjured implicitly by a document write or explicitly by capture, and the two collide), and the export treats the file as authoritative for fields the database owns. The fix is to make **features and projects explicit first-class resources** — each with one deliberate creation door — make **documents children that require their parent to exist**, and keep the **database canonical** with the export as a faithful mirror. The feature listing is also prepared for search, and the disk importer is folded onto the same single write path.

One constraint shapes the rollout. This is a **shared service** many agents hit at once, some of them loading the skills mid-change. So the contract changes are **signposted before any breaking phase lands** (Phase 0), so an agent that trips over a surprise gets a clear, recoverable hint rather than an unexplained failure.

## Scope

In scope: tidying the agent-submission + tracker + export surface into a coherent resource model.

- An explicit, **strict** create for a feature that carries notes (error if it already exists); notes are edited afterwards through the existing dedicated note-update operation.
- Document writes that require their feature (and project) to already exist — no silent seeding.
- A single-feature read endpoint (status / owner / notes), and a single-project read.
- Explicit project creation with **automatic project creation removed entirely**; operations referencing an unknown project return a clear error.
- The export reading only from the database — retiring merge-preservation — with the editorial *Suggested order* stored as a free-text field on the project (this subsumes the former `tracker-suggested-order` feature).
- Folding the disk importer (the “walker”) onto the single API write path so there is one doc-write implementation and the importer honours the same explicit-create contract.
- The feature listing extended with query parameters so search can drop in later.
- Resolving the doc-id vs logical-path split for comments and synthesis into one documented scheme.
- Reordering the one mis-ordered skill (`feature-context`) to create the feature before writing its first document.

The trust model is unchanged throughout: localhost, single-user, no auth.

## Vision

Every feature, project, and document is an explicit resource
with one front door; the database is canonical and the repo snapshot mirrors it; and the
surface is clean enough that the MCP facade — and concurrent agents mid-change — meet a
coherent, self-explaining contract.

## Non goals

- **The MCP facade itself.** This feature precedes it; it does not build it. HTTP stays the v1 surface.
- **Any change to the trust model.** Localhost, single-user, no auth throughout.
- **Preserving intra-state row order in the export.** A from-DB render orders rows by the database, not by hand-curated position. The webapp can offer human-friendly tracker views; the exported `features.md` is read mainly by agents, who don't need editorial row order. Accepted, including the one-off churn across existing feature trackers.
- **A feature-dependency DAG.** Modelling features' dependencies (and the richer “compute the next-best feature” loop that `feature-choice` would drive from them) is a separate, larger piece of work — captured as its own feature, not folded in here. Here, *Suggested order* is just free text.
- **Reworking the lifecycle verbs.** `claim` / `ship` / `park` / `release` / `drop` / `note` keep their current contract; only the create/existence story changes.
- **Building the search UI.** We prepare the listing's read shape for search; the search experience is later work.

## User stories

1. As a workflow agent registering a feature, I want to create it with its notes as a deliberate first step, so that the notes are never lost — today `feature-context` writes the context document first, which silently seeds an empty row, and the later register call errors and drops the notes.
2. As a workflow agent writing a document, I want the write to fail loudly if the feature or project doesn't exist yet, so that a mistyped slug or wrong project returns a clear error instead of quietly conjuring a phantom row — the error is itself a useful signal to double-check I'm talking to the right place.
3. As an agent that loaded the skills before this change shipped, I want a clear, self-explaining hint when I hit a newly-tightened contract, so that when a document write now returns an error I haven't seen before, the response tells me what changed and how to recover — rather than leaving me confused mid-feature.
4. As an agent or developer about to act on a feature, I want to read a single feature's status, owner, and notes directly, so that I can check-before-create or see who already owns it without fetching and filtering the whole project list.
5. As the tracker maintainer, I want the *Suggested order* stored in the database, so that the exported `features.md` is a faithful snapshot rendered from canonical state — today it is hand-edited prose the export must carefully preserve, and any DB-only field silently disagrees with the file.
6. As an agent preparing for feature search, I want to filter the feature listing by text and status, so that a future webapp search and the MCP facade can ask “in-progress features matching *export*” without each caller re-filtering the full list.

## Data model

No new user data and no change to the trust boundary — this is a coherence pass over the existing `projects` ⇒ `features` ⇒ `documents` hierarchy. What changes:

| Resource | Stored state | Change |
|---|---|---|
| Project | name, repo path, **+ Suggested-order free text** | Gains an explicit creation door and a read; **implicit creation is removed**. Carries the editorial Suggested-order as a free-text field. |
| Feature | slug, status, owner, notes (+ `created_at`, already stored) | Created explicitly and strictly (error if it already exists), carrying notes; no longer seeded as a side effect. Notes edited later via the existing note operation. |
| Document | unchanged (logical-key identity, versioned content) | Write requires the feature *and* project to exist; one write implementation, shared with the importer. |

Relationships: a document requires its feature; a feature requires its project. The implicit “create the parent on demand” behaviour is removed at every level in favour of one explicit door per resource.

The *Suggested order* is modelled as a **free-text field on the project**, not a structured list — it is editorial prose (closer to an epic note) that the export renders verbatim as the `## Suggested order` section in its canonical position between the Available and Done sections. Intra-state row order is deliberately not modelled (see Non-goals). Exposing each feature's existing `created_at` in the export is a small addition worth considering, since created-time plus the Suggested-order text is about all an agent reader needs.

## Technical approach

The shape is a consistent *parent must exist; create it through one explicit door* rule across all three levels, with the database as the single source of truth, and one write path shared by the API and the importer.

### One strict front door per resource

Features and projects each gain an explicit creation operation. Create is **strict**: creating something that already exists returns a clear error (consistent with how an unknown project or feature is reported — an error is a useful signal to re-check, not a footgun to paper over). Feature creation carries notes; later note edits use the existing dedicated note-update operation. We deliberately do *not* make create idempotent/create-or-update — that was considered and rejected in `tracker-feature-notes-update` because a create that also edits can silently overwrite a shipped feature's outcome note and blurs the verb's meaning.

### No implicit creation anywhere

The document write stops seeding the feature and stops upserting the project; it returns a clear error if either parent is missing. `capture` likewise stops conjuring the project. Projects are created explicitly — a rare, deliberate act. Because we own the consuming skills, `feature-context` (the one skill that writes a document before registering) is reordered to create-first. This is the change that couples a webapp release to a skills release and must land in lockstep.

### One write path, shared with the importer

Today the disk importer (“walker”) carries its own copy of the document-write logic, which the API path duplicates. The importer — which runs only as a manual bootstrap/import CLI, never at runtime — is reduced to a thin disk-reader over the same write path, collapsing the duplication. Because the importer is a **bulk migration tool**, the disk layout is treated as an explicit declaration: it creates the projects and features it finds (in dependency order) and then submits their documents; only an *orphaned* document — one whose parent directory is absent — is rejected. The no-implicit-creation rule governs the *running service* (which never conjures a parent); the importer is an explicit bulk loader, run by hand, where the on-disk tree *is* the declaration. Its realistic use is a one-off migration or two, so this keeps that practical while preserving the single write implementation.

### Signposting before breaking (Phase 0)

Before any contract-tightening phase, additive groundwork lands so concurrent or freshly-loaded agents aren't blindsided. The **load-bearing hint is the error body** for the newly-required-parent cases — it carries the actionable, specific recovery message right at the point of failure. Generic *forewarning* comes from a `notices` field on the manifest endpoint, which the authoring skills read *per-write, mid-flow*, so an already-loaded agent sees it immediately before the breaking call; the same field on the tracker listing reaches the human webapp UI and `feature-choice`, but not the authoring agents. A short transition note also goes in the repo. The aim is simply that any agent hitting a surprise gets *some* recoverable hint.

### Database-canonical export

The export (which lives in the feature-skills skills repo, not this webapp) stops merging into the existing file and renders purely from database state — rows ordered by the database, the Suggested-order free text rendered verbatim. Retiring merge-preservation is what makes the DB genuinely canonical.

### Listing prepared for search

The HTTP feature-listing endpoint (which today takes no query string) gains query parameters — at least a text match over slug/notes and a status filter — landing the read-shape change now so a future search endpoint, and the MCP facade, compose cleanly.

### Coherent comments / synthesis addressing

Comments and synthesis are read (and integrated) by logical feature path but written by numeric document id. The requirement is to resolve that split into one documented scheme. Since only the human webapp UI writes these today (agents only read and integrate), the likely outcome is to bless document-id as the canonical write key and document the asymmetry as intentional rather than add endpoints nobody calls.

## Alternatives

1. Forgiving (idempotent) feature create tracker-feature-notes-update, Alternatives; raised again in review round 1 Rejected (Option A chosen): a create that doubles as create-or-update can silently overwrite a shipped feature's outcome note and blurs what “create” means. Strict create plus the existing note-edit operation keeps one meaning per verb.
2. Leave the walker alone (W1) Original non-goal; review round 1 Rejected: once the API stops auto-creating parents, the walker becomes the lone path that still conjures them from disk — re-introducing the very incoherence this feature removes, and leaving the write-logic duplication in place.
3. Retire the disk-walk entirely (W2) Review round 1 Rejected: it throws away the bootstrap/import capability we want to keep. W3 (thin importer over the single write path) preserves bootstrap while removing the duplication.
4. Preserve intra-state row order in the export skills-api-cutover's “merge, not render” decision; review round 1 Declined as a non-goal: the exported tracker is agent-facing and row order doesn't matter to that reader; the webapp can carry human-friendly views instead. Authorised the one-off churn this causes.
5. Model Suggested order as a structured list / dependency DAG Review round 1 (requirements comment) Deferred: the richer vision — a feature-dependency DAG that `feature-choice` composes with free text to compute the next-best feature — is its own feature. Here, Suggested order is a free-text field.
6. Wrap the MCP facade over the current surface without tidying Original sequence (mcp-facade context) Rejected: wrapping typed tools over the accreted shape would harden today's footguns into the tool contract. This feature exists to avoid that.

## Delivery phases

### Phase 0 — Signpost the coming changes

Purely additive, ships first, no behaviour change. The actionable, load-bearing hint is a self-explaining error body for the soon-to-be-required-parent cases (delivered at the point of failure in Phases 2–3). Forewarning comes from a `notices` field on the manifest endpoint — read per-write, mid-flow, so an already-loaded agent sees it right before the breaking call; the same field on the tracker listing reaches the human UI and `feature-choice`, not the authoring agents. Plus a repo transition note. The notices value is a static transition message, cleared in a later cleanup once the cutover lands. **Testable:** the manifest carries the populated transition notice until cutover; no existing behaviour changes.

### Phase 1 — Feature as a first-class resource

Explicit strict create-with-notes for features (error if exists) plus the single-feature `GET`. Webapp-only; nothing yet requires it. **Testable:** creating a new feature returns it with notes; creating an existing one errors; GET returns status/owner/notes.

### Phase 2 — Documents require their feature

Remove the silent feature seeding from the document write; 404 on an unknown feature with a self-explaining body; reorder `feature-context` to create-first. The reordered skill treats an “already exists” response to its create-first call as *resumption* — it fetches the feature and continues if it's a benign re-run (still available / its own), refreshing notes via the note verb if needed, and surfaces only a genuine unexpected collision. Lockstep webapp+skills. **Testable:** a document write to an unknown feature 404s; after the reorder, the context flow creates the feature (with notes) then writes the doc and notes survive; and a re-run of a partially-completed context flow resumes rather than erroring.

### Phase 3 — Explicit projects, no implicit creation

Explicit project create + single-project GET; remove auto-project from both the document write and `capture`; operations on an unknown project return a clear error. **Testable:** creating a feature or writing a document under an unknown project errors; an explicit project create then succeeds.

### Phase 4 — One write path — fold in the importer (W3)

Reduce the walker to a thin disk-reader over the single write path and remove the duplicated write logic. The importer is a bulk migration tool: it creates the projects and features it finds on disk (in dependency order) as an explicit bulk declaration, then submits their documents — the running service still never creates parents implicitly. **Testable:** importing a tree into an empty DB bootstraps it (parents created from disk) and yields the same rows as authoring via the API; a document whose parent directory is absent (orphaned) is rejected.

### Phase 5 — Suggested order in the DB; export reads only from the DB

Add the Suggested-order free-text field on the project with a write path; retire merge-preservation in the export (feature-skills repo) so `features.md` renders purely from canonical state. Cross-repo. **Testable:** a second render of unchanged DB state is byte-identical to the first (renderer idempotence — not continuity with the pre-cutover file, which deliberately changes); a notes value set only in the DB appears in `features.md`; and the Suggested-order text renders as the `## Suggested order` section in its canonical position between Available and Done.

### Phase 6 — Listing prepared for search

Extend the HTTP feature-listing endpoint with query parameters (text over slug/notes, status filter). **Testable:** the endpoint filters by text and by status and composes the two.

### Phase 7 — Coherent comments / synthesis addressing

Resolve the doc-id vs logical-path split. The likely outcome — documenting doc-id as the canonical write key (only the webapp UI writes these today) — **folds into Phase 0's repo transition note rather than a standalone phase**; this becomes a real phase only if we decide to add logical-path write endpoints. **Testable (only if it lands as code):** the chosen addressing is exercised by a test and the documented contract matches behaviour.

## Indicative notes

Plan-level pointers worth carrying forward, not binding:

- Silent seeding is `submit_document` calling `upsert_feature` and `upsert_project` (call sites `feature_skills_webapp/storage/documents.py:151-152`); Phase 2/3 remove those and add the errors. The *function bodies* live in `walker.py:162,170` and are shared with `walker._process_file` (`:261-264`) — that shared duplication is exactly what Phase 4 collapses.
- `capture` raises `FeatureExists` → 409 today and also calls `upsert_project` (`feature_skills_webapp/storage/tracker.py:98-101`); Phase 3 removes the project upsert.
- The single-feature read wraps the existing `get_feature` accessor (`feature_skills_webapp/storage/tracker.py:38`); the idempotent note edit already exists as `update_feature_note` (`:265`).
- The *HTTP* feature-listing endpoint takes no query string today; the storage `list_features` is already project-scoped — Phase 6 adds text/status params at the HTTP layer.
- The export's merge-preservation is `_merge_features_md` in the **feature-skills** repo (`bin/feature-html-to-md`); Phase 5 changes this repo and that one together.
- The walker runs only via the `feature-skills-import` CLI (`feature_skills_webapp/cli.py`), never at startup; its write logic (`walker._process_file`) is the duplication that Phase 4 collapses onto `submit_document`.
- Only `feature-context` writes a document before registering; `feature-requirements` already claims first, `feature-plan` only writes, and the review skills are read-only — so the lockstep skill change is one skill.

## Design notes

Decisions captured during requirements review (round 1):

1. **Strict create, not idempotent**
  Feature/project create errors if the resource exists (Option A); notes are edited via the existing note operation. Respects `tracker-feature-notes-update`'s prior rejection of an edit-on-create verb.
2. **Automatic creation removed at every level**
  No implicit project or feature creation; children error on a missing parent. An error is treated as a useful signal to re-check, consistent across projects and features. Creating a project is a rare, explicit act.
3. **Intra-state row order is a non-goal**
  The export renders rows in database order. The exported tracker is agent-facing and row order doesn't matter to that reader; human-friendly ordering lives in webapp views instead. One-off churn across existing feature trackers is accepted.
4. **Suggested order = project free text**
  Modelled as a free-text field on the project, rendered verbatim by the export. The richer feature-dependency DAG (and the `feature-choice` “next-best feature” loop) is deferred to its own feature.
5. **Walker reduced to a thin importer (W3)**
  The disk importer becomes a thin reader over the single write path, removing the duplicated write logic, while preserving the bootstrap/import capability. (The walker's deeper future role is still open and to be revisited.)
6. **Importer is an explicit bulk loader (Option A)**
  The importer treats the on-disk tree as an explicit bulk declaration: it creates the projects and features it finds (in dependency order); only an orphaned document (parent directory absent) is rejected. The running service still never creates parents implicitly — the importer is a by-hand bulk loader, whose realistic use is a one-off migration or two (potentially of pre-dev-store content), so disk-as-declaration is the pragmatic choice.
7. **Signpost before breaking (Phase 0)**
  Self-explaining error bodies, a `notices` channel on already-read endpoints, and a repo note — kept deliberately simple, since any recoverable hint beats none for an agent hitting a surprise mid-change.
