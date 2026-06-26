# api-coherence — Context

## Problem space

The agent-submission HTTP surface (documents + tracker) and the features export grew incrementally across roughly six shipped features. It works, but it has accreted incoherences — and the planned `agent-submission-mcp-facade` would otherwise wrap typed MCP tools straight over the accreted shape, hardening it. This feature tidies the whole surface into one coherent resource model *first*, so the MCP facade can wrap something clean.

Two concrete symptoms triggered it, both hit while capturing `agent-submission-mcp-facade` itself:

- **Silent feature seeding + lost notes.** `PUT /api/documents/{project}/{feature}/…` calls `upsert_feature` as a side effect, creating the tracker row (status `available`, no notes, no event). The subsequent `POST …/capture` — the only verb that carries notes — then finds the row, raises `FeatureExists`, returns 409, and the notes are dropped on the floor. The standard "write the doc, then register it" ordering loses the notes every time.
- **Export/DB divergence.** The `--merge-features` export preserves the existing `features.md` prose columns rather than reading canonical DB state, so a notes value that *is* set in the DB never reaches the repo snapshot — the two silently disagree.

These are symptoms of one root: **feature existence has no single front door, and the export treats the file as authoritative for fields the DB owns.** The fix is a coherent model — features are explicit first-class resources, documents are children that require them, and the DB is canonical with the export mirroring it.

## Related work

This cleans up the agent-submission arc rather than extending it:

- **`agent-submission-api`** — logical-key document endpoints (`web/submit.py`, `storage/documents.py`), the source of the silent `upsert_feature` seeding.
- **`agent-submission-tracker-ops`** — the tracker endpoints and verbs (`web/tracker.py`, `storage/tracker.py`): capture/claim/park/release/ship/drop/note. `get_feature` already exists (drop uses it), so single-feature GET is a thin wrapper; `drop` is a reversible soft-archive.
- **`versioned-content-store`** — the `logical_key` identity that makes path-free addressing possible.
- **Subsumes `tracker-suggested-order`.** Making the export DB-only requires the editorial Suggested order to gain a DB home + write path — today it is hand-edited prose the export only preserves. That is exactly that feature's scope, so it folds in here (and the standalone is being dropped from the tracker).
- **Precedes `agent-submission-mcp-facade`.** Sequenced before it so the MCP tools wrap a coherent surface.

The feature-* skills (`feature-context`, `feature-requirements`, `feature-plan`, the review skills) consume this API; we own them, so reordering their calls (create → PUT) is in-scope, not a blocker.

## Constraints

The agreed direction (decisions taken in the capture conversation, to be refined in requirements):

- **Idempotent feature create, with notes.** Creation becomes re-runnable and carries notes — no strict create-or-409 footgun in the normal path.
- **Documents require their feature.** `PUT /api/documents/…` 404s for an unknown feature instead of silently seeding it. One explicit front door to feature existence.
- **Single-feature GET.** `GET /api/projects/{p}/features/{f}` returning status/owner/notes — supports check-before-create and notes inspection; thin wrapper over the existing `get_feature`.
- **Export reads only from the DB.** Retire the merge-preservation behaviour entirely; the export is a faithful snapshot of canonical DB. Requires giving Suggested order a DB representation + write path (the subsumed `tracker-suggested-order` scope).
- **Unify addressing.** The doc-id path (`/doc/{id}/comments`, `/doc/{id}/synthesis-response`) stays as a lookup convenience, but the same operations should also be reachable by logical feature path — or both share one inner implementation. Drop the implicit skill-facing vs webapp-internal split.
- **Projects get the same treatment.** An explicit/idempotent creation door, consistent with the feature model, rather than only-ever-implicit creation.
- **Prepare `list_features` for search.** Extend the listing (query params — text match over slug/notes, likely a status filter) so the webapp can search features later; land the read-shape change now so search drops in cleanly.
- **Trust model unchanged** — localhost / single-user / no-auth. **Phaseable** — large, but decomposes into independent slices.

## Links

- Subsumes: `tracker-suggested-order`.
- Precedes / unblocks: `agent-submission-mcp-facade`.
- Builds on: `agent-submission-api`, `agent-submission-tracker-ops`, `versioned-content-store`.
- Surfaced: capturing `agent-submission-mcp-facade` (2026-06-27) — the capture-409 and export-divergence snags.
- Skills affected: `feature-context` / `feature-requirements` / `feature-plan` and the review skills (feature-skills repo).

## Open questions

- **Verb shape.** Does idempotent create keep the name `capture`, or split into a pure `create` (idempotent, notes) plus the existing `note`? What happens to the `feature_captured` event on a no-op re-create?
- **Project parity.** Full parity (explicit create + GET + 404-on-missing for child features), or a lighter touch? Should creating a feature 404 on an unknown project, mirroring documents-require-feature?
- **Unified addressing.** Add logical-path comment/synthesis write endpoints, or refactor both to share one inner impl and keep doc-id as the only public write path? Who actually writes these — only the human webapp UI today, and will MCP agents too?
- **Suggested order in the DB.** How is it represented (an ordered column on `features`? a separate ordering table?) and what is its write path (a reorder API verb)?
- **Search.** Which fields are searchable (slug / notes / owner / doc content?), and is it a `list_features` query-param extension or a new search endpoint? How does it compose with the existing project filter?
- **Migration & sequencing.** Existing `capture` callers (skills, tests) and anything relying on current export semantics — how do the skill updates sequence with the API change across phase boundaries?
- **Walker scope.** Is the walker's implicit Stage-1 disk-import `upsert` in or out? It is being retired by `skills-api-cutover` — does this feature touch it or leave it alone?
