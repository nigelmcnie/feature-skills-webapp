# agent-submission-api

## Problem

The feature workflow is still Claude-shaped and file-shaped at its only write path. To create or update a doc, an agent must emit a complete, template-correct standalone HTML file into `~/.claude/feature-docs/<project>/<feature>/` and wait for the webapp's filesystem walker to import it. The round-trips that feed the workflow back to the agent — synthesis responses and comments — are addressed by absolute path: `GET /synthesis-response?path=<absolute>`, `GET /comments?path=<absolute>`, `POST /comments/integrate {path, ids}`.

Concretely, this means:

- Every agent carries a copy of the doc CSS/JS and must produce byte-correct, template-shaped HTML — the thing F1 and F2 were built to make unnecessary.
- Documents are addressed by absolute `~/.claude/…` paths, baking the Claude dev-store location into the contract a second agent would have to adopt.
- The walker doesn't reliably notice a new file until prompted — the skills already work around this by firing `POST /admin/discover` before polling.

Nigel now runs Codex alongside Claude and wants the same workflow there. Porting Codex onto today's protocol would bake a second agent into the same Claude-owned path and standalone-HTML write contract. F1 ([versioned-content-store](../versioned-content-store/context.html)) and F2 ([server-rendered-docs](../server-rendered-docs/context.html)) have already cleared the way: the webapp now holds canonical *structured* content (versioned ordered `(key, body)` sections), renders docs itself, and owns the section manifest. The one thing still missing is a way for an agent to *write* a doc other than dropping an HTML file on disk.

## Vision

Any local agent creates and updates feature docs — and reads back the human's comments and synthesis — by stable logical identity over HTTP, never by writing HTML files or knowing a filesystem path.

## Scope

The long-term cross-agent contract is broad (the context lists doc writes, tracker mutations, comment/synthesis reads, listing, exports, change metadata). This feature draws the v1 line at **the document read/write substrate the eventual Claude/Codex wrappers need to run the authoring loop**, and defers the rest.

**In scope (v1):**

- Create or update the agent-authored doc types — context, requirements, plan, and feedback — by logical identity, supplying section *bodies* rather than a whole HTML document. Each submit is a **full replacement** of the doc's section set (opaque types — feedback — submit a single body). The webapp versions and renders them.
- A **validate-only (dry-run)** submission mode that checks the payload against the manifest without writing — cheap insurance for an unfamiliar wrapper.
- Read a doc's current content, its active comments, and its synthesis responses by logical identity; mark comments integrated by logical identity.
- Expose the section manifest (the full spec — shape, ordered section labels, repeated-section prefixes) for a doc type, so an agent knows which sections to supply without embedding any presentation knowledge.

**Deferred to a dedicated follow-up feature:** typed tracker mutations (claim / move / ship a feature) and listing of projects / features / documents. These were considered for v1 but cut — see Alternatives and Design notes. The tracker stays mutable via the dev-store `features.html` file in the meantime.

## Non-goals

- **Tracker mutations and listing.** Deferred to a dedicated follow-up feature (see Scope) — keeping them out avoids a second source of truth against the parsed `features.html`.
- **A typed MCP surface.** HTTP is the v1 contract. An MCP facade is a thin shim over the same operations and is added later (the context's "layer them, don't choose").
- **Rewriting the Claude skills or building Codex wrappers** to consume the API. Those are separate features (cross-agent plan steps 5–6). This feature delivers the substrate they will call.
- **The final dev-store cutover / one-shot migration.** A later feature. The walker and the path-keyed endpoints persist as compatibility through the transition.
- **Archival or deletion of API-native docs.** A path-less doc can't be archived or removed by any current mechanism, and v1 deliberately doesn't add one (see Design notes) — the file-based archive still applies during the transition.
- **Typed export operations.** File-based `feature-html-to-md` export for opted-in repos stays as it is during the transition.
- **Richer structured section input** (markdown, per-item JSON). Section bodies are opaque trusted HTML fragments, matching F1's stored model.

## User stories

1. As an agent authoring a doc
  I want to submit each section's body by section key and have the webapp render and version it
  The requirements skill finishes drafting. Instead of writing requirements.html to the dev-store, it submits

  keyed by project + feature, and the doc appears in the inbox — no template, no copied CSS/JS.
2. As an agent iterating on a doc
  I want to read the human's comments and synthesis responses by logical identity
  After the human submits the synthesis form, the skill reads the synthesis for

  and the comments for

  , folds them in, re-submits the doc, and marks the comments integrated — all by logical key, never an absolute path.
3. As an agent that doesn't know a doc's structure
  I want to fetch the section manifest for a doc type
  A Codex wrapper that has never seen the Claude templates asks the API what sections a plan doc has, gets the ordered labels plus the

  repeated-prefix, and can validate its payload with a dry-run before committing.
4. As Nigel running two agents mid-migration
  I want file-import and API-submit to converge on the same document
  A doc first imported from a dev-store file is later updated via the API. Because both resolve the same logical key, the API update versions the existing row instead of forking a duplicate.

## Data model

No new tables. The API writes through the existing spine F1 established; what changes is which rows the operations touch and how they are addressed, not the structure. The load-bearing relationships:

- **Convergent identity.** A doc is addressed by logical identity — (project, feature-or-none, doc type, instance) — the *same* key the walker derives from a path. File-import and API-submit must resolve to the same row; that convergence is what makes the eventual cutover clean and is the single most important correctness property here.
- **Agent attribution.** A submit records a new content version attributed to the originating agent (the existing per-version `actor`, which the walker sets to `importer`) — the seam that later lets the inbox explain *who* changed a doc.
- **Full-replacement writes.** A submit carries the complete section set for the doc; the webapp replaces the prior content wholesale (omitting a section removes it). It does not patch individual sections — there is no read-modify-write.
- **Version-on-change preserved.** Re-submitting byte-identical content cuts no new version and produces no inbox noise (reuses F1's content equality). For the API path this content-equality is the change gate; the walker's file size/mtime gate is irrelevant (there is no file).
- **Instance is agent-supplied.** The instance number (only ever > 1 for feedback docs) is provided by the caller, which already knows its round; the server does not auto-assign it. Context/requirements/plan are always instance 1.
- **Auto-creation and status.** Submitting a doc for an unseen project/feature creates those rows (as the walker's upserts do); an API-created doc starts `active` and carries no `source_path`. Its inbox title is server-derived from feature + doc type (no `<title>` tag exists to read).
- **Concurrency.** Concurrent writes to one logical key serialise on the existing single-writer lock; resolution is last-writer-wins (no merge, no optimistic-concurrency token in v1).
- **Comments / synthesis_responses** are read and written by logical identity instead of `source_path`, resolving through the document row.

## Technical approach

**Logical-key HTTP endpoints on the existing Starlette app, additive.** The new operations sit alongside the current routes; the path-keyed reads and the int-id POSTs stay as compatibility through the transition rather than being replaced in this feature.

**Address by logical identity, not path and not row id.** Operations identify a doc by its identity components (project / feature / doc type / instance) — not the absolute `~/.claude` path (the coupling we're retiring) and not the opaque integer row id (which an agent *creating* a doc can't know and which isn't stable across a re-import). The exact URL/JSON shape is a plan concern.

**Section bodies are opaque trusted HTML fragments**, exactly as F1 stores them; the webapp renders them in manifest order (F2 already does this, so submission order is irrelevant). Opaque doc types submit a single body. Agents stop producing `<head>` / `<style>` / `<script>` / template chrome entirely.

**Validation.** Submitted section keys are validated against the doc type's manifest: an unknown key is rejected (the manifest is the contract — fail fast to catch wrapper bugs), while a missing expected section is tolerated (the manifest already marks many sections optional and the renderer tolerates gaps). The dry-run mode runs exactly this validation and returns the verdict without writing.

**Manifest exposure.** The webapp serves its own manifest spec for a doc type — shape, ordered `(key, label)` section labels, and repeated-section prefixes (so a wrapper authoring a plan knows `phase-*` sections are dynamic) — answering the question F2 explicitly deferred to this feature. The manifest stays single-sourced in the webapp; no agent embeds a copy.

**Responses.** A submit returns enough for the agent to close the loop — the logical key, the assigned document id, the new version number, and the rendered `/doc/{id}` URL — so it can link the human straight to the inbox view. Reads mirror the existing endpoints' codes: a known doc with no data yet returns an empty result (not 404); an entirely unknown logical key returns 404; bad input is 400; the DB-unconfigured case is 503.

**Reuse the storage primitives, don't fork a second write path.** Writes go through the same parse/serialise, version-recording, single-writer transaction, and SSE broadcast the walker uses. (See Indicative notes for the specific seams.)

**Trust and locality.** The app is localhost-only and single-user; F2's trust model is accident-prevention over a trusted self-authored corpus, and section bodies are rendered as opaque trusted HTML. v1 adds no auth token — a conscious call resting on a stated assumption: a single trusted local user with no hostile local processes. If that assumption ever changes (multi-user, shared host), the decision re-opens deliberately.

## Alternatives considered

1. Include tracker mutations (claim/move/ship) in v1
  Source: context open question 5; review round 1
  Cut to a follow-up feature. Feature rows are currently

  by parsing

  ; a typed op writing rows directly creates a second source of truth that the next tracker parse would overwrite — the same dual-management hazard we avoid for documents, but unsolved for the tracker. It needs its own design, and Phases 1–2 already deliver the substrate the wrappers need.
2. Patch/partial section updates instead of full replacement
  Source: review round 1
  Rejected. A patch requires read-modify-write of the current version — something the walker never does — which would quietly become the "second write path" this approach exists to avoid. Full replacement keeps the API as "the walker minus its file-gate" and matches how the skills already rewrite the whole doc each iteration.
3. Server-assigned feedback

  (next-N)
  Source: review round 1
  Rejected. The agent already knows its round number; auto-assignment adds hidden server state and an extra round-trip for no benefit.
4. MCP-first contract for v1
  Source: context open question 1; cross-agent plan
  The webapp already speaks HTTP; an MCP shim is a thin typed layer over the same operations and can be added without redesign. Starting HTTP-first avoids committing to an MCP server topology before the operation set has settled. The plan says as much: prefer MCP only "if both agents can use it cleanly; otherwise start with local HTTP endpoints and wrap them later".
5. Keep addressing docs by absolute path, or by integer row id
  Source: the shipped skill-webapp-integration endpoints
  Path-keying bakes the

  dev-store location into the cross-agent contract — the coupling this feature retires. The integer row id isn't knowable to an agent

  a doc and isn't stable across a re-import. Logical identity is the stable, agent-derivable key F1 introduced for precisely this.
6. Accept richer structured section input (markdown / per-item JSON)
  Source: design tradeoff
  F1 stores opaque HTML fragments and F2 renders them; a second authored representation means a server-side converter and a second source of truth. Prefer simplicity — HTML fragments now, richer input later only if authoring proves painful.

## Delivery phases

### Phase 1 — Submit documents by logical identity

Create-or-update a context / requirements / plan / feedback doc by logical identity, supplying the full section set (a single opaque body for feedback). Auto-creates project / feature / document rows; records a version attributed to the originating agent; cuts a version only on real content change; derives the title; emits created/updated events and an SSE refresh. Validates section keys against the manifest (reject unknown, tolerate missing) and offers a validate-only dry-run. Returns the logical key, document id, version number, and inbox URL. **Testable value:** an agent can author and revise a doc end-to-end without writing a file, and it renders in the inbox.

### Phase 2 — Read round-trips by logical identity

Logical-key reads for a doc's current content, its active comments, and its synthesis responses; mark-comments-integrated by logical key; expose the section manifest for a doc type. Reads of a known-but-empty doc return empty rather than 404; unknown keys 404. Path-keyed endpoints retained as compatibility. **Testable value:** the full author → comment → read → revise → integrate loop is drivable entirely by logical key.

Tracker operations and listing are **out of scope** for this feature — see Scope and Alternatives.

## Indicative implementation notes

Plan-level detail worth carrying forward — not requirements, but the seams are concrete enough to record so the plan doesn't re-derive them.

- **Reuse the key derivation, don't re-implement it.** The API must call the existing `logical_key()` / `feedback_type()` / `feedback_instance()` in `storage/walker.py` (`{project}/{feature or '-'}/{doc_type}/{instance}`) so file-import and API-submit converge. Add a test asserting a file-imported doc and an API-submitted doc of the same identity land on one row.
- Reuse `parse_content`/`serialise` (`storage/doc_content.py`), `record_version`/`current_content` (`storage/versions.py`), `manifest_for`/`ManifestSpec`, and the `transaction()` + `broadcaster.broadcast()` pattern. The submission handler is close to the walker's `_process_file` minus the stat/mtime gate and the `source_path` handling.
- Manifest exposure should serialise the full `ManifestSpec` — `shape`, ordered `section_labels`, and `repeated_prefixes` — not just `(key, label)`. A required/optional flag per section is a cheap extra if the spec can express it.
- **Reconcile-safety test.** Pin that an API-native (path-less) doc is never marked `missing` by a reconcile walk that doesn't see it. The current walker reconcile uses `source_path NOT IN (seen)`, and SQL `NULL NOT IN (...)` is never true, so path-less rows are already excluded — but that safety is easy to break with a later `COALESCE(source_path,'')` "cleanup", so it needs a test.
- Validation/limits: mirror the existing pre-transaction validation and 1 MB per-value guards in `web/synthesis.py` / `web/comments.py`. Compatibility endpoints to keep: `GET /synthesis-response?path=`, `GET /comments?path=`, `POST /comments/integrate {path, ids}`.
- Manifests are unversioned (hardcoded in `doc_content._MANIFESTS`); a wrapper that fetched the manifest could in principle skew against a later change. Acceptable for single-user v1 — noted, not addressed.

## Design notes

- **Tracker ops deferred** (round 1): cut to a follow-up feature to avoid a second source of truth against the parsed `features.html`; v1 is the doc read/write substrate the wrappers need.
- **Full-replacement, not patch** (round 1): keeps the API as "the walker minus its file-gate" and avoids a divergent read-modify-write path.
- **Feedback = write + read** (round 1): the API writes the feedback doc body (Phase 1, opaque) and reads its synthesis responses (Phase 2); the human's responses still arrive via the existing browser form.
- **Agent-supplied instance** (round 1): the agent already knows its round; server auto-assignment would add hidden state.
- **Archival left unsolved deliberately** (round 1, Nigel): simplest plan — little else is in flight during development, the "can't archive a path-less doc" case is tolerable, and the file-based archive still applies during transition. Revisit at cutover.
- **No auth in v1** (round 1): on the stated assumption of a single trusted local user with no hostile local processes; re-open if that changes.
- **Server-derived title; content-equality change gate** (round 1): deriving the title from feature + doc type is fine; the API path gates on content equality rather than file size/mtime.
- **Validate-only mode kept** (round 1): cheap, and de-risks the Codex wrapper finding its feet.

## Review decisions

### Round 1 (post-merge review)

No correctness defects found in the feature code; all four keystone properties (shared-key convergence, three write states, reconcile-safety) verified and non-vacuously tested. Items actioned:

- **Fixed — plan manifest missing `verification`:** the plan template ships a `verification` section but the `plan` manifest never listed the key, so the corpus test flagged this feature's own plan doc and the section rendered out of order. Added `("verification", "Verification")` to the manifest (after `file-structure`, before `qc`). Pre-existing F2 drift surfaced by this feature; one line, turns the suite green.
- **Fixed — non-string `actor` silently coerced:** the PUT handler did `body.get("actor") or "agent"`, so a non-string actor was stored verbatim. Now rejected with 400, matching how every other body field is validated.
- **Added tests:** an opaque `requirements-feedback` write→read round-trip (the opaque write path + GET branch + instance>1 were untested), and a project-level (`feature='-'`) read seeded directly (pins the `'-'`→None read mapping).
- **Declined — empty-`ids` integrate still emits a `comment_integrated` event:** faithfully copied from the sibling `comments.py` handler, so it's consistent, not a regression; tightening one would mean tightening both — out of scope here.
- **Declined (flagged for retro) — the corpus test reads out-of-repo dev-store state:** `test_corpus_section_keys_subset_of_manifest` scans `~/.claude/feature-docs` and `skipif`s when absent, so it's non-deterministic across machines/CI — a real F1 test-quality smell, but not this feature's to fix.
