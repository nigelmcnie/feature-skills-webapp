# checklist-state — Context

## Problem space

Plan-checklist tick state lives inside the checklist section's HTML, so every full-section PUT is a clobber vector: any writer that re-renders sections from a stale read silently erases ticks other writers made since. Observed for real on waddles/pipeline-domain-layer: the orchestrator's contract-section syncs erased the implementer's phase-1/phase-2 checkmarks mid-run; the implementer happened to notice during final verification and had to do a consolidating re-check of all 38 items. Two agents writing one plan doc is now the normal case — an orchestrator syncing contract/decision sections while an implementer ticks items — and continuous implementation (feature-implement-all, captured alongside) maximises tick frequency while other writes are in flight.

Earlier feedback had already proposed a small tick endpoint; it was never captured durably and so never became a feature. This capture makes it one.

## Related work

- **feature-plan / feature-implement checklist contract** — stable `data-checklist-item` ids (mint-new-never-renumber), the single-line `<li><input>` adjacency the unchecked-item detector greps for.
- **feature-implement-all** (feature-skills, captured alongside) — the mode that maximises this contention; its doc-ownership open question dissolves if this ships.
- **The documents API** — full-section PUT is the only write primitive today.

## Constraints

- **An endpoint alone is insufficient while ticks stay in the section body** — the incident's clobber was a full-section PUT re-sending stale checklist HTML; an endpoint writing into that same body still loses to the next PUT. Tick state must move server-side (keyed by `data-checklist-item` id) and be merged into the rendered checklist, so a section PUT physically cannot clobber it. The section body becomes template; the server owns checked state.
- Item ids are already contractually stable across re-renders, which is what makes server-side keying viable.
- Renders and exports (`feature-html-to-md`, the doc view, the unchecked-item detector's input) must all reflect the merged state, or the workflow reads stale ticks.
- feature-implement switches from section re-PUT to the tick endpoint for check-offs.
- Optional secondary, not the main event: optimistic version guard on section PUTs (PUT carries the version read; stale write → 409) for the general writer-vs-writer case.

## Links

- waddles retro findings run 78 — “Webapp document writes race between concurrent agents”.
- feature-implement-all context (feature-skills) — the sibling capture.

## Open questions

- Migration: existing docs carry checked state embedded in stored HTML — parsed into server state on first touch, or migrated wholesale?
- Does the unchecked-item detector move from grepping HTML to querying the API?
- What happens to tick state when a plan re-render mints new item ids mid-feature?
- Should ticks carry actor attribution (agent vs human) for the audit trail?
- Plan-only, or should any checklist-bearing doc type get the same mechanism?
