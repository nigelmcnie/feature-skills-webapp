# skills-api-cutover

## Problem space and motivation

This is the last mile of the structured-content arc. The arc's stated finish line was: dev-store-is-truth → DB-with-importer-bridge (F1) → server-side render + native interaction (F2) → agents write via an API, then delete the dev-store (F3 + this). F1, F2, and F3 have all shipped — and the **entire server-side contract for the end-state already exists**. What's missing is that **nothing uses it**.

Concretely, F3 ([agent-submission-api](../agent-submission-api/context.html)) landed the full logical-key API, confirmed in `web/app.py`'s routes: `PUT /api/documents/{project}/{feature}/{doc_type}/{instance}` (write a doc by logical key — no file, no path), the matching `GET`, the comments/synthesis round-trips under `/api/documents/.../{comments,synthesis}`, and `GET /api/manifests/{doc_type}` — so the section manifest is now *fetchable*, the "authoritative, not re-copied" resolution to F3's open question. Yet every `feature-*` skill still **writes standalone HTML files** into `~/.claude/feature-docs/<project>/<feature>/` and relies on the webapp's filesystem walker to import them (the webapp still `_watch`es and `request_walk`s the dev-store on startup). No skill references `/api/documents` at all.

So today the DB is the source of truth for **reading and rendering** (versioned, structured, server-rendered), but the dev-store is still the source of truth for **authoring**: skills write files → walker imports → DB. This feature closes that last gap — cut the skills over to authoring through the API, run one final import, and retire the dev-store. The capability is all built; this is the adoption.

Two payoffs land with it. First, it's the **cross-agent finish line**: once skills author via the API and fetch the manifest, the same operations work for Codex — Claude and Codex skills become thin wrappers over one contract (codex/plan.md steps 5–6), depending on nothing in `~/.claude`, no HTML templates, no watcher timing. Second, it **kills two recurring problems for good**: the template-vs-manifest drift (the `verification`-section incident — see agent-submission-api's context — can't recur once skills fetch the manifest instead of copying templates), and the file-watcher reliability gap (observed twice this session: new docs invisible until a manual `/admin/discover`) — with explicit API writes there is no walk to wait on; the write *is* the event.

## Related work

- **[agent-submission-api](../agent-submission-api/context.html) (F3, shipped — prerequisite).** Built the entire surface this feature consumes: `PUT/GET /api/documents/...`, comments/synthesis by logical key, and `GET /api/manifests/{doc_type}`. This feature is its first and only client; F3's own open question on how the manifest reaches a submitting agent is answered by skills fetching it here.
- **[versioned-content-store](../versioned-content-store/context.html) (F1, shipped).** Gave the importer/walker its idempotent, re-runnable design — explicitly so the dev-store could be ingested one last time at cutover. That property is what makes the "final import then delete" step safe.
- **[server-rendered-docs](../server-rendered-docs/context.html) (F2, shipped).** Moved rendering and the manifest into the webapp, so an authoring agent deals only in structured sections and never in presentation — the precondition for skills to stop emitting HTML.
- **agent-submission-tracker-ops (queued, Available).** The tracker-mutation slice of the same API (claim/move/ship a feature; list projects/features/documents). Related because the skills' tracker edits are part of what cuts over — see the sequencing question below.
- **The cross-agent plan ([codex/plan.md](file:///home/nigel/codex/plan.md)).** This feature is its steps 5–6: build thin wrappers around the shared contract, then migrate skills off direct dev-store writes with one final import.
- **The skills themselves** (`~/src/nigelmcnie/feature-skills/feature-*/SKILL.md`) and `docs/webapp-polling.md` — the file-writing + path-keyed-polling flows this feature rewrites.

## Constraints and considerations

**Cross-repo, mostly skills-side.** The bulk of the work is in the **feature-skills** repo — rewriting each `feature-*` SKILL.md flow to `PUT /api/documents` and fetch `/api/manifests` instead of writing template HTML to the dev-store. The **webapp** side is cleanup: remove the walker / `_watch` / `request_walk` / `/admin/discover`, the legacy path-keyed endpoints (`/synthesis-response?path=`, `/comments?path=`), and any iframe `/raw` remnant. Both repos must move in a coordinated order.

**Sequence: cut over, prove parity, then delete.** Don't delete the dev-store until the API-authored path is proven equivalent to the file+walker path. The importer's idempotency (F1) means a final reconciling import is safe; the deletion is the irreversible step and should come last, behind a parity check.

**The manifest becomes the single source.** Skills fetching `/api/manifests/{doc_type}` at authoring time is what structurally ends template-vs-manifest drift — there's no second copy to drift. This is the concrete realisation of the "webapp owns the manifest" decision from F2.

**Exports must regenerate from the DB.** Today `feature-html-to-md` reads dev-store HTML; with no dev-store, repo exports (still gated by `.feature-workflow.toml`) have to be produced from DB content instead. Exports stay optional per-repo; only their *source* changes.

**Dependencies.** F1 + F2 + F3, all shipped. Possibly gated on agent-submission-tracker-ops for the tracker half (open question).

**Self-referential, so test on something safe.** The skill being rewritten is the same machinery used to capture this very doc. A botched cutover could break the authoring loop itself — favour a path where the file-based flow keeps working until the API flow is proven, rather than a hard swap.

## Links

- Prereq F3 (the API): [agent-submission-api](../agent-submission-api/context.html)
- Prereq F1 / F2: [versioned-content-store](../versioned-content-store/context.html), [server-rendered-docs](../server-rendered-docs/context.html)
- Cross-agent plan: [codex/plan.md](file:///home/nigel/codex/plan.md) (steps 5–6)
- API surface to consume: `feature_skills_webapp/web/app.py` (routes), `web/synthesis.py` + `comments.py` (legacy path-keyed endpoints to drop)
- Walker/importer to retire: `feature_skills_webapp/web/discovery.py`, `storage/walker.py`
- Skills to rewrite: `~/src/nigelmcnie/feature-skills/feature-*/SKILL.md`, `docs/webapp-polling.md`

## Open questions

1. **Parity verification before deletion.** How do we prove the API-authored path produces equivalent DB state to the file+walker path before deleting the dev-store? A dual-run that diffs the resulting versions/sections? This gates the irreversible step.
2. **Tracker cutover dependency.** Claim/move/ship today work by skills editing `features.html` (a doc the walker parses into rows). Does this feature depend on **agent-submission-tracker-ops** landing first, or can doc-authoring cut over while tracker mutations stay file-based for one transition?
3. **One-shot import after the live walker is gone.** Do we keep an import command (for ingesting an existing repo's `docs/` into the DB) as a standalone migration tool, or is import deleted along with the walker and dev-store?
4. **Exports without a dev-store.** Post-cutover, what generates the repo markdown/HTML from the DB — a new webapp export endpoint or CLI — and who triggers it (the skill right after a write? a git hook? a manual command)?
5. **Scope of the per-skill rewrites.** Each `feature-*` skill builds a different doc type; how much per-skill change to assemble section payloads from the fetched manifest and PUT them, plus moving the interactive reads (comments/synthesis) onto `/api/documents/.../{comments,synthesis}` and retiring the polling in `webapp-polling.md`?
6. **Codex wrappers — here or after?** Is standing up the Codex `~/.codex/skills/feature-*` wrappers part of this feature, or a follow-up once the Claude skills are proven on the API?

## Addendum — scope handed over from agent-submission-tracker-ops (2026-06-20)

The [agent-submission-tracker-ops](../agent-submission-tracker-ops/requirements.html) requirements round (with Nigel) deliberately drew that feature's boundary *narrow*: it ships only the additive API substrate, and explicitly hands the risky, walker-coupled work to this feature. This resolves open question 2 (tracker cutover dependency) and sharpens question 4 (exports). What tracker-ops delivers, and what this feature must therefore own:

**What tracker-ops provides (substrate this feature consumes):**

- **Typed tracker mutations** over HTTP, by logical identity: `claim` (available → in_progress, sets owner), `ship` (in_progress → done, records the outcome into the single `notes` column), and `capture` (create an *available* feature row only — the context *document* is created separately via `PUT /api/documents`). No generic "move" and no "reopen" — Nigel confirmed he does not un-ship features. Mutations are transition-gated (a no-op write emits no event), write the `features` table, emit a feature-level event (`document_id = NULL`, `{project, slug}` payload; ship reuses the existing `shipped` event), and SSE-broadcast.
- **Listing** endpoints: projects; a project's features (slug/status/owner/notes); a feature's documents (doc_type/instance/logical_key/version/url).
- A status invariant held at the application level — a one-off backfill of existing NULL-status rows to `available` plus every create path setting a valid status (the old `upsert_feature` created NULL-status rows that fell out of every webapp bucket). No DB `CHECK` — SQLite can't add one without a full table rebuild, judged not worth it; a Postgres move someday could add the constraint then.

**What this feature must own (moved here because tracker-ops cannot make it safe alone):**

- **The walker-authority flip.** Retiring `_apply_tracker_rows` in `storage/walker.py` so the `features` table stops being overwritten by the parse of `features.html` — making the table authoritative. This is the change that makes tracker-ops's mutations actually stick; until it lands, a re-walk re-derives feature rows from the file (last-writer-wins), so the mutations coexist with the parse but aren't yet clobber-safe. It belongs here because it is only safe *after* the feature-* skills migrate (next point), which is this feature's job.
- **Migrate the feature-* skills' tracker edits to the mutation API.** Today `/feature-context` (capture), `/feature-requirements` & `/feature-plan` (claim), and the ship step all hand-edit `features.html`. They must call `claim`/`ship`/`capture` instead. **Gate the flip behind this**: retire the parse only once every skill that writes the tracker has cut over, or claims will silently vanish (no error) — the stranding hazard. Prefer a mechanical check (mirroring the parity gate) over prose.
- **Export repoint with fidelity — merge, not render.** Post-flip, regenerating `features.md` from the DB cannot be a from-scratch render: the table models neither intra-state row order nor the prose **"Suggested order"** section, so a naive render silently drops both (and the bad output lands in committed git history). Regeneration must *merge* — rewrite only the table-modelled rows, preserving editorial ordering and prose untouched (the same opaque-region instinct F1 uses for feedback bodies). This answers open question 4's fidelity dimension.
- **Resolve the stale tracker-document view.** Once the table is authoritative, the project page shows live rows while the indexed `features.html` *document* at `/doc/{id}` still renders the old hand-edited HTML — two disagreeing views. The webapp tracker view should render from the table; the opaque-body tracker document is retired with the walker.
- **Keystone test.** Pin the anti-clobber property where the flip lands: a direct mutation survives a subsequent walk. (It must fail before the flip and pass after.)

**Net:** tracker-ops = additive, self-contained API (listing + mutations, shippable and testable in isolation even while the parse still runs). This feature = the authority flip, the skill migration that makes it safe, the export-from-DB repoint (merge-not-render), and the tracker-view cleanup. Between the two, the entire tracker deliverable is achieved.
