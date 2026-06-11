# versioned-content-store

## Problem space and motivation

The webapp can tell you a doc *changed*, but never *what* changed or *why*. The inbox's "New since last visit" surfaces any active doc with an event newer than `read_state.last_read_at` (`storage/inbox.py` `new_since_last_visit`); opening it stamps it read. During implementation or review, an agent re-saves `requirements.html` or `plan.html`, the walker notices the mtime/size change and emits a bare `updated` event (`storage/walker.py` `_process_file`), and the doc bubbles back into the list with no indication of what moved. You click in, re-skim the whole thing, and think "haven't we done this already?" — the recurring pain that kicked this off.

The fix we want downstream is a flagged inbox plus a toggleable diff view, so a re-surfaced doc tells you the reason and lets you read just the delta. But that's impossible today: **there is no content history to diff against.** The `documents` table stores only metadata — title, size, mtime, `source_path` — and docs are served live from disk by iframing `/doc/{id}/raw` (`web/doc_view.py`). The `content_html` column from migration 0002 was laid down as a "Stage 2 seam" and has never been populated.

This feature is the foundation that makes everything downstream possible: move document content *into* the database as **structured, versioned content**, so a diff is "two stored versions" and a change reason is "which sections differ". It is the first of a deliberate split. **F1 (this feature)** lands the content model and a re-runnable importer, purely additively — rendering and interaction are untouched. **F2 (its downstream consumer)** switches rendering to server-side from the DB, drops the iframe, and re-homes comments/synthesis natively. The flagged-inbox + diff view is a separate, later feature that needs only F1 + F2.

The shape mirrors how `read-state` shipped: plumbing in place and proven before any consumer surfaces it.

## Related work

- **[doc-discovery](../doc-discovery/context.html) (shipped).** Built the walker this feature evolves. It scans the dev-store, gates re-reads on mtime+size, and emits `created`/`updated`/`archived`/`missing`/`reactivated` events with a `payload_json`. F1 is essentially the walker upgraded to parse section structure and store versioned content — the natural place this logic grows, not a parallel system.
- **[read-state](../read-state/context.html) (shipped).** The direct precedent for the sequencing: a self-contained plumbing feature that landed and was tested before its consumer (`inbox-view`/`doc-view`) existed. F1 follows the same "additive, not yet surfaced" pattern.
- **[doc-view](../doc-view/context.html) (shipped).** Owns the current rendering path: `/doc/{id}` shell + `/doc/{id}/raw` iframe served live from `source_path`, preferring `content_html` when present. F1 leaves this entirely alone; F2 replaces it.
- **[synthesis-response-capture](../synthesis-response-capture/requirements.html) and [skill-webapp-integration](../skill-webapp-integration/requirements.html) (shipped).** Both reach *into* the rendered doc's DOM through the iframe to make features work — synthesis reads `.your-thoughts textarea` / `.tier-routine`; comments read `window.__fsComments` (`web/doc.html`). That scraping only works because the templates are rigid, and it's the fragility A2 is meant to retire. F1 doesn't touch it; it just makes the cleaner alternative possible.
- **The feature-skills templates.** The doc structures F1 must parse live at `~/src/nigelmcnie/feature-skills/feature/{context,requirements,plan,feedback,features}-template.html`. They have stable section IDs (e.g. requirements: `problem`, `vision`, `user-stories`, `data-model`, `technical-approach`, `alternatives`, `delivery-phases`…), with rich-text bodies and a few repeating structured lists. The skills tell agents to copy the template verbatim and fill content in, which is what makes them reliably parseable.

## Constraints and considerations

**A2 was chosen deliberately.** Three options were weighed: (A1) raw HTML in the DB, (A2) structured content in the DB rendered server-side, and (B) a git-backed dev-store. A2 won because it's the only one that solves all three problems at once — free diffs, an explicit "why did it change" reason channel, and retiring the iframe-DOM-scraping fragility. B keeps the coarse mtime walker and doesn't solve the reason problem; A1 keeps template-drift fragility.

**The schema must not move when document structure does.** The golden-path layout is stable now but will be rearranged, trimmed, and extended over time. So the model is generic: `{doc_type, ordered sections[{key, body}]}` with a per-type *section manifest* for the expected layout. Adding, removing, or reordering sections is a content/manifest change — never a schema migration.

**Section bodies are opaque, trusted HTML fragments.** Only the skeleton (which sections, in what order) is structured; bodies are stored whole. This preserves a section's freedom to drop in a diagram or arbitrary markup, and keeps us out of the business of modelling prose. Trust model is unchanged — content comes from our own agents, not untrusted users — so this isn't a sanitisation question.

**Cut a version only on real change.** A re-scan or resubmission with identical content must be a no-op — no new version, no event, no inbox noise. This is the whole point; a version that adds nothing is exactly the "haven't we done this already?" annoyance, relocated.

**The importer is re-runnable and idempotent, and it replaces the walker's change detection.** It parses every doc type — including the `features.html` tracker — from existing dev-store HTML into the structured model, reconciling current files into the DB and cutting versions only where content actually changed. Re-running is safe and is the planned cutover mechanism: run it one last time, flip the skills over to submitting directly, done.

**Import seeds a single current version; history accrues from then on.** On-disk files carry no history, so back-filling real version history isn't possible (and we're not git-mining the dev-store). This is accepted as the only way it can work.

**The tracker is dual-represented.** `features.html` is ingested and versioned like any doc (its body fits the opaque-HTML model fine) *and* its rows are still extracted into the `features` table for the inbox's in-progress / shipped queries. This duality already exists in the walker today; F1 keeps it.

**feature-skills does not change in F1.** This is a webapp-only feature. The skills keep writing files to the dev-store exactly as they do; the importer ingests them. Switching the skills to submit content directly (via MCP) is a later cutover feature, and is what eventually lets the dev-store be deleted, with per-repo exports as optional outputs.

**Design for cross-agent direct writes — without building them yet.** A parallel goal has emerged: make this workflow usable from Codex as well as Claude (see the cross-agent plan in Links). The eventual integration point is a later feature (agents submit structured updates through an API/MCP instead of writing files), but F1 should make the cheap, additive schema decisions now that *don't foreclose* it. The load-bearing one: **decouple document identity from `source_path`**. Today a document's identity effectively *is* its path — the unique index is on `source_path` — which is the deepest `~/.claude`-coupling in the schema and the thing that makes any non-file writer impossible. F1 should give documents a stable **logical identity** (the importer derives it from the path; a future API supplies it directly — same key both ways, so import-from-file and submit-via-API converge on the same row and the cutover stays clean), make `source_path` a derived/optional input with the unique index moved to the logical key, and record the originating **agent/actor** in the change metadata (the same "why did it change" channel, now also "which agent"). Note this also removes the file-watcher-timing fragility entirely: an explicit write *is* the event, with no walk to wait on.

**Test discipline is inherited.** No-network (`pytest-socket`), xdist with a per-worker DB, exercise against a temp DB and the `transaction()` helper, per existing storage conventions.

## Links

- Design doc: [feature-skills webapp design](file:///home/nigel/src/nigelmcnie/feature-skills/docs/webapp.html) — §4 data model, §6 feature cards.
- Templates: `~/src/nigelmcnie/feature-skills/feature/{context,requirements,plan,feedback,features}-template.html` — the section structure to parse.
- Walker to evolve: `feature_skills_webapp/storage/walker.py`
- Current rendering: `feature_skills_webapp/web/doc_view.py` (note the unused `content_html` seam from migration 0002).
- Inbox consumer: `feature_skills_webapp/storage/inbox.py`
- Precedent: [read-state context](../read-state/context.html) — plumbing-before-consumer.
- Cross-agent plan: [codex/plan.md](file:///home/nigel/codex/plan.md) — Codex's analysis of the F1/F2 migration as the path to dual Claude+Codex use.

## Open questions

1. **Section-parser fragility (the main risk).** The importer must extract the *authored* section bodies out of templates that also embed comment-rail machinery (`comment-trigger`, `comment-popover`, `rail-list`) and synthesis interaction widgets. How do we cleanly separate authored content from template chrome, and how does the parser fail when a doc doesn't match its manifest — skip, store-as-one-opaque-blob, or flag for attention? This is where the feature most likely breaks; F1 should prove it across the whole existing corpus.
2. **Version + content table shape.** One `document_versions` table holding a per-version content snapshot, vs sections as first-class rows per version? Snapshot-per-doc keeps diffing simple (diff two snapshots, scope per section); confirm that's enough and that nothing downstream needs per-section version identity.
3. **What "content equality" means for the no-op check.** Byte-equality of the serialised sections, or a normalised compare (whitespace, attribute order)? Too strict and trivial reflows cut noisy versions; too loose and real edits get swallowed.
4. **Tracker manifest.** The tracker's body is a table of rows, not prose sections. Does it ride the generic model as a single opaque-body "section", or get a small bespoke manifest? Confirm the dual representation (versioned opaque content + extracted rows) stays clean either way.
5. **Does the importer fully supersede the walk, or run alongside it?** During F1 the iframe still renders from disk, so both the old metadata path and the new content/version path may need to coexist. Decide whether F1 rewires the existing walk in place or adds the versioning as a second pass over the same scan.
6. **How is logical document identity represented?** A composite of existing columns (`project`, `feature`, `type`) vs a single logical-id string — note the templates already carry a convention, `const docId = 'docs/features/<feature>/context'`. Either way, feedback docs break the singleton assumption: they're `<phase>-feedback-<N>`, multiple per phase, filename-typed with no meta tag, so they need an instance discriminator in the key. This is the decision that makes a future Codex/API writer possible, so it's worth getting right in F1 even though F1 itself only writes via the importer.
