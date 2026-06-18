# agent-submission-api

## Problem space and motivation

The whole workflow is currently Claude-shaped and file-shaped. Skills live under `~/.claude/skills/feature*`, write standalone HTML documents into `~/.claude/feature-docs/<project>/<feature>/`, and the webapp discovers them with a filesystem walker. Two pressures converge on the same fix. First, **cross-agent use**: Nigel now runs Codex too and wants the same feature workflow available there — but a deep Codex port today would just bake a second agent into the Claude-owned dev-store path and the standalone-HTML write protocol. Second, the **fragility of the file protocol itself**: agents have to emit complete, template-correct HTML; the webapp scrapes interaction state back out of an iframe; and documents are addressed by absolute `~/.claude/...` paths. The file-watcher even fails to notice new docs until a manual re-walk (observed in practice). None of that is a good substrate for a second agent.

F1 ([versioned-content-store](../versioned-content-store/context.html)) and F2 ([server-rendered-docs](../server-rendered-docs/context.html)) clear the way: after them the webapp holds canonical structured content, renders it itself, and owns the section manifest. At that point agents no longer need to produce HTML or know how docs are presented. **This feature is the real cross-agent integration point: a contract by which any agent submits structured document updates to the webapp** — addressed by stable logical identity, not file paths — after which Claude and Codex skills become thin workflow wrappers around the same operations. The dev-store demotes to a migration/input compatibility layer; repo exports stay optional.

**Receipt — the drift this retires, observed in the wild (2026-06-18).** The plan section manifest lives in the webapp (`storage/doc_content.py`) while the plan *template* lives in the feature-skills repo — two hand-synced copies. They matched when F1 first transcribed the manifest (2026-06-12), then drifted independently: the manifest gained `data-model`/`contract` from the live corpus (retro-findings-capture, 06-14), and the template gained a `verification` section (feature-skills `64693df`, 06-17) that nothing propagated to the manifest — so a generated plan with that section turned `test_corpus_section_keys_subset_of_manifest` permanently red until the key was hand-added (`0ebb3e7`, 06-18). That is exactly the failure this feature removes: once agents author *against* the webapp-owned manifest instead of copying a template, a template can't drift from the manifest because the manifest is the only source. It is concrete motivation to resolve open question 3 toward "the manifest is fetched/authoritative", not "re-copied per agent".

## Related work

- **[versioned-content-store](../versioned-content-store/context.html) (F1, prerequisite).** Introduces the structured content model and — critically for this feature — the **logical document identity** decoupled from `source_path`. The importer derives that identity from the path; this API supplies it directly. They must be the *same* key so file-import and API-submit converge on the same row, which is what makes the eventual cutover clean.
- **[server-rendered-docs](../server-rendered-docs/context.html) (F2, prerequisite).** Moves rendering and the section manifest into the webapp, so submitting agents deal only in structured sections — never presentation. The decision that the **webapp owns the manifest** (and the shared workflow spec) is what lets the per-agent wrappers be thin. Note (per F2's round-1 requirements review): F2 makes the webapp the manifest *owner* and uses it only internally to render — **exposing the manifest to external/submitting agents is deferred to this feature (F3)**. So the open question below ("How is the manifest exposed to a submitting agent?") is F3's to answer, not F2's.
- **[skill-webapp-integration](../skill-webapp-integration/requirements.html) (shipped).** Already moved synthesis/comment round-trips from clipboard to HTTP — but via *path-keyed* endpoints (`/synthesis-response?path=<absolute>`, `/comments?path=<absolute>`). Those are exactly the `~/.claude`-coupled, agent-specific endpoints this feature supersedes with logical-key addressing; they likely stay as legacy/compat for one transition.
- **The cross-agent plan ([codex/plan.md](file:///home/nigel/codex/plan.md)).** Codex's own analysis, which lands on this same sequence: don't deeply port skills yet, do F1 then F2, then add the submission API as the integration point, then build thin Claude/Codex wrappers, then migrate Claude skills off direct file writes with one final import.

## Constraints and considerations

**Hard dependency on F1 + F2.** This is the third step in the arc. It needs F1's logical identity and structured model, and F2's webapp-owned rendering + manifest. Do not start it before both land; do not pull pieces of it forward into F1/F2 beyond the cheap, additive "don't foreclose it" schema decisions already noted there.

**Logical-key addressing, never absolute paths.** Every operation addresses documents/features by stable logical identity (the identity F1 establishes), so a Codex wrapper can create or update a doc without knowing anything about `~/.claude`, HTML templates, or watcher timing. The existing `?path=<absolute>` endpoints are the anti-pattern to retire.

**HTTP core, thin MCP facade — layer them, don't choose.** The webapp is already a Starlette HTTP app, so the contract lives there as logical-key HTTP endpoints; an MCP surface is a thin typed shim over the same operations that both Claude and Codex can mount. Starting HTTP-first and wrapping with MCP later is fine; the requirement is one set of operations, not two implementations.

**Agents stop writing complete HTML.** The defining change: agents submit *structured* document updates (doc type + sections), and the webapp renders. The template-rendering logic must not be duplicated inside any agent.

**Shared workflow policy is agent-neutral and single-sourced.** The manifest (webapp-owned, per F2) plus the workflow spec are the contract both agents follow; neither Claude's nor Codex's wrapper should carry its own copy. Wrappers hold only agent-specific glue (Claude slash commands / `CLAUDE.md` / Agent-tool conventions vs Codex's equivalents).

**The operation surface is broad.** Per the plan, it spans more than doc writes: list projects/features/documents; create-or-update context/requirements/plan/feedback/tracker docs; claim, move, and ship tracker rows; read active comments and mark them integrated; read synthesis responses by logical key; export markdown/HTML for opted-in repos; and record change metadata (including originating agent) so the inbox can later explain why a doc changed. Scoping which of these are v1 vs later is itself a task for requirements.

**Cutover, not big-bang.** The endgame (a later step): run one final import from the dev-store, flip the Claude skills from file-writes to API-writes, and keep dev-store / file export only as compatibility or optional output. Existing path-keyed endpoints and the walker can persist through the transition.

## Links

- Cross-agent plan: [codex/plan.md](file:///home/nigel/codex/plan.md) — the analysis that prompted this feature.
- Prereq F1: [versioned-content-store](../versioned-content-store/context.html) (logical identity, structured model).
- Prereq F2: [server-rendered-docs](../server-rendered-docs/context.html) (webapp-owned rendering + manifest).
- Endpoints to supersede: `feature_skills_webapp/web/synthesis.py`, `web/comments.py` (path-keyed).
- Design doc: [feature-skills webapp design](file:///home/nigel/src/nigelmcnie/feature-skills/docs/webapp.html)

## Open questions

1. **MCP-first or HTTP-first for v1?** Both agents can mount MCP; the webapp already speaks HTTP. Which is the v1 surface, and is the MCP shim in-process in the webapp or a separate server?
2. **What's in the v1 operation set vs later?** The full surface is broad (doc writes, tracker mutations, comment/synthesis reads, exports). Likely v1 is "write the four doc types + read comments/synthesis by logical key"; tracker mutations and exports may follow. Requirements should draw that line.
3. **How is the manifest exposed to a submitting agent?** Does the agent fetch the section manifest from the API at authoring time, or is it published as an agent-neutral spec doc the wrapper reads? This determines how an agent knows which sections a given doc type expects without embedding presentation knowledge.
4. **Locality and auth.** The webapp binds `127.0.0.1:8800` for a single user. Is localhost-only trust enough for write operations, or does cross-agent write access want even a token? (Probably the former, but worth a conscious call.)
5. **Do tracker mutations belong in this contract?** Claiming/moving/shipping features is currently inferred from parsing `features.html`. Should agents mutate tracker rows through typed operations here, or keep editing the tracker doc (now DB-backed) and let extraction follow?
6. **How thin can the wrappers actually get, and where do they live?** With the webapp owning the manifest + spec, what's genuinely left in a Claude vs Codex wrapper beyond invocation glue — and is the agent-neutral spec hosted by the webapp, or kept as docs the wrappers reference?
