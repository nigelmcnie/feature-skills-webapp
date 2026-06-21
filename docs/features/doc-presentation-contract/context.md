# doc-presentation-contract — Context

## Problem space

Surfaced while debugging a webapp 500: viewing the `kea / enforcement-altitude` requirements doc showed a table with no borders, padding, or header weight — and other documents have the same unstyled look in places. It is not just tables.

The skills-api-cutover moved document styling away from self-contained HTML files (each carrying its own `<style>`) toward skills posting HTML *fragments* that the webapp renders, with one webapp-owned stylesheet (`web/static/doc.css`) as the **sole** source of styling. `extract_safe_inner` (in `storage/doc_render.py`) deliberately strips author `<style>`/`<head>`/`<script>` as accident-prevention, since the chrome now shares the DOM with the rendered body.

But `doc.css` was only ever populated with the chrome plus the handful of structural classes that existed at port time. The skills emit a far richer, still-growing vocabulary that has no rules: `<table>`/`<th>`/`<td>`, `h4`, `blockquote`, `hr`, and dozens of semantic classes (`.actor`/`.want`/`.scenario`, `.alt-*`, `.vision-statement`, `.stories`, `.questions`, …). Those fall back to bare user-agent defaults on the dark theme, which reads as "missing formatting".

Measured across the live corpus: 147 `sections`-shape docs depend entirely on `doc.css`; 7 `opaque`-shape docs additionally still carry `<style>` blocks that get stripped on render. Root cause is **contract drift**: nothing grounds skill output in an available CSS vocabulary, and there is no feedback loop for when a document needs something the webapp has never styled (the first time a diagram is requested, say).

## Related work

This is the unfinished edge of **server-rendered-docs** (F2) — which made the deliberate choice to centralise styling into webapp-owned `doc.css` and to strip author `<style>` for accident-prevention. That decision is sound; what was missed is that the canonical stylesheet was only partially populated and there is no contract binding skill output to it. F2's own open question ("how is the webapp-owned presentation layer structured?") was only half-answered.

The **skills-api-cutover** manifest endpoint (`GET /api/manifests/{doc_type}`, in `web/submit.py`) is the existing discovery channel skills already fetch — the natural place to point skills at the presentation contract.

Direction the conversation converged on (recorded as leaning, not locked requirements): (1) widen `doc.css` to cover the vocabulary the corpus actually uses and *comment each block with its intended use*, so the served file is simultaneously the styling and its own contract — one source of truth, nothing to drift against; (2) treat author CSS as a *scoped escape hatch* rather than stripping it — documents may submit optional `extra_css`, stored with the content version and rendered, with opaque docs' own `<style>` scope-and-kept instead of dropped; (3) a promotion ratchet — `extra_css` usage is logged and surfaced in **feature-retro** as "this needed bespoke CSS, promote into doc.css?", so novel content renders correctly day one and recurring needs graduate deliberately.

## Constraints

- **Scoping is mandatory.** Any respected author/extra CSS must be confined to the doc container (`#doc-main` / `main.document`) via CSS `@scope` or selector-prefixing. It must not bleed into the shell chrome (breadcrumbs, comment rail, sticky action bar) — that bleed is exactly why F2 strips `<style>` today. Keeping the CSS is a deliberate, scoped reversal of that decision, not an undo of it.
- **Avoid re-introducing drift.** The whole bug is a second source of truth diverging from the stylesheet. Prefer the served CSS itself as the contract over a separate vocabulary file that can fall out of sync.
- **The escape hatch must be advertised.** If skills don't know `extra_css` exists, they either hallucinate unknown classes (today's silent breakage) or refuse to render novel content. The contract has to say "here's the vocabulary; if you need more, send scoped extra_css and it'll be flagged for review."
- **Trust model stays accident-prevention.** The corpus is self-authored on localhost; this is about stopping accidental style bleed, not hostile-author hardening.
- **Cross-repo.** Webapp side = widen/document doc.css, scope-and-keep CSS, store + log extra_css, expose the contract. Skill side (the separate `feature-skills` repo) = fetch the contract, ground fragments, submit extra_css when needed. The repo split is a downstream implementation detail.
- **Past and future both in scope.** Widening canonical doc.css fixes the 147 existing section-docs; scope-and-keep recovers the 7 opaque docs; the hatch covers future novel content.

## Links

- Design notes from the originating conversation: `~/.claude/plans/agile-tinkering-pizza.md`
- Worked example of the bug: requirements doc 268 (`/doc/268`), `kea / enforcement-altitude` — clean `<table>` markup, no matching CSS.
- Upstream context: `docs/features/server-rendered-docs/` (the centralise-styling + strip-style decision) and `docs/features/skills-api-cutover/` (the manifest/API discovery channel).

## Open questions

- Scoping mechanism: CSS `@scope` (clean, single known browser on localhost) vs selector-prefixing (more portable, needs a small CSS rewrite step). Which, and is there a parsing/validation burden?
- Where does `extra_css` live — a column on the content version, a side table, or part of the section payload? How does it version alongside content?
- How is extra_css usage logged and surfaced to feature-retro — an `events` row, a tracker field, something the retro skill queries?
- How is the contract served to skills — raw commented `doc.css` fetched directly, a pointer from the manifest endpoint, and/or a thin derived index? Keep it to one source of truth.
- How much canonical widening to invest in now vs leaning on the extra_css hatch for the long tail?
- Do the 7 opaque docs with stripped `<style>` need a one-off backfill/re-scope, or do they render acceptably from widened canonical CSS alone?
