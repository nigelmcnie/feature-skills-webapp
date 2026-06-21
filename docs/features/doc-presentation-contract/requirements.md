# doc-presentation-contract — Requirements

## Problem

Document styling moved (in **server-rendered-docs**, then **skills-api-cutover**) away from self-contained HTML files — each carrying its own `<style>` — toward skills posting HTML *fragments* that the webapp renders, with one webapp-owned stylesheet (`web/static/doc.css`) as the **sole** source of styling. `extract_safe_inner` deliberately strips author `<style>`/`<head>`/`<script>` as accident-prevention, now that the chrome shares the DOM with the rendered body.

But `doc.css` was only ever populated with the shell chrome plus the handful of structural classes that existed at port time. The skills emit a far richer, still-growing vocabulary that has no rules. This is the measured corpus inventory — referenced by later sections rather than re-listed: `<table>`/`<th>`/`<td>`, `h4`, `blockquote`, `hr`, definition lists, and dozens of semantic classes (`.actor`/`.want`/`.scenario`, `.alt-*`, `.vision-statement`, `.stories`, `.questions`, …). These fall back to bare user-agent defaults on the dark theme, which reads as "missing formatting".

The bug surfaced while viewing the `kea / enforcement-altitude` requirements doc (`/doc/268`): clean `<table>` markup, no borders, no padding, no header weight. It is not just tables. Measured across the live corpus: **147 `sections`-shape docs** depend entirely on `doc.css`; **7 `opaque`-shape docs** additionally still carry `<style>` blocks that are stripped on render. The root cause is **contract drift**: nothing grounds skill output in an available CSS vocabulary, and there is no feedback loop for the first time a document needs something the webapp has never styled.

## Scope

This feature is **webapp-side**. It owns the presentation contract and the rendering behaviour.

**In scope:**

- Widen and document the canonical `doc.css` to cover the vocabulary the corpus actually uses.
- Serve that stylesheet to skills as the discoverable contract, at a stable URL.
- Accept, store, scope, and render an optional `extra_css` escape hatch; scope-and-keep opaque docs' own `<style>` instead of dropping it, which recovers their styling on a **best-effort** basis (no dedicated per-doc backfill).
- Log `extra_css` usage so recurring needs can be surfaced for promotion.

**Out of scope:**

- Skill-side consumption — the separate `feature-skills` repo fetching the contract, grounding fragments in it, and submitting `extra_css`. That is a downstream dependency, tracked but not built here.
- Surfacing logged usage in the feature-retro skill (the webapp logs; the retro skill reads — cross-repo). This feature ships logging only; see Design notes.
- Hostile-author hardening — the trust model is unchanged (see Non-goals).

## Vision

Every skill-authored document renders correctly the first time, because the webapp serves a single commented stylesheet that is simultaneously the styling and its own contract — and anything that stylesheet doesn't yet cover is a scoped, logged escape hatch, never silent breakage.

## Non goals

- **Not a security boundary.** The corpus is self-authored on localhost. This is about stopping accidental style bleed, not defending against a hostile author. `extra_css` scoping is correctness, not a sandbox.
- **Not a second vocabulary artifact.** No separate JSON/markdown catalogue of class names. A second source of truth diverging from the stylesheet is exactly the drift that caused this bug.
- **Not a parallel styling channel.** `extra_css` is a deliberately rare, promotion-bound escape hatch for novel content — the expectation is that usage is small and shrinking as recurring needs graduate into the canonical stylesheet, not that documents routinely carry bespoke CSS.
- **Not a restyle of the shell chrome.** Breadcrumbs, comment rail, and the sticky action bar are untouched; the contract governs the document body only.

## User stories

1. As a document reader
  I want skill-authored docs to render with proper tables, headings,
  and semantic styling on the dark theme
  Opening

  today shows a borderless,
  unpadded table that reads as broken; it should look deliberate.
2. As a skill author (agent)
  I want a single fetchable contract that tells me the CSS vocabulary
  the webapp will style
  While drafting a requirements doc, the agent needs to know that

  and

  exist and
  how user-story cards are marked up — rather than guessing class names.
3. As a skill author (agent)
  I want a scoped escape hatch when I need styling the contract
  doesn't cover, instead of hallucinating unknown classes or refusing to render
  The first time a doc needs a callout box or a diagram with no
  matching class, the agent submits scoped

  and it renders
  correctly on day one.
4. As the maintainer running retros
  I want bespoke

  usage surfaced so recurring
  needs graduate into the canonical stylesheet deliberately
  Three docs over a fortnight each shipped extra_css for a
  callout; the retro flags "this keeps needing bespoke CSS — promote into doc.css?".

## Data model

No new tables are required by the data relationships; what matters is where the new state lives relative to existing content.

- **`extra_css` is an optional top-level field on the document write** (a sibling to `sections`/`body` in the PUT payload), stored *within the document's content version*. Because it travels in the same immutable version as the body it styles, it can never drift from that body and its history is preserved per version. It is not a mutable side-property.
- **Absent and empty are identical.** A missing `extra_css` field and an empty/whitespace-only one both mean "no extra CSS" — nothing is stored and no usage event is recorded. The field is bounded by the same size limit as section bodies.
- **Usage is recorded as an event.** Each time a document is written with non-empty `extra_css`, that fact is recorded against the document in the existing events stream, so the retro skill can later query "which docs needed bespoke CSS, and how often" without scanning content blobs.

The canonical `doc.css` itself is not modelled in the DB — it is a served static asset and the single source of truth for the vocabulary.

## Technical approach

Two tiers plus a feedback ratchet, all webapp-side.

### 1. Canonical, documented `doc.css` as the served contract

Widen the stylesheet to cover the vocabulary inventoried in *Problem*, and **comment each block as an explicit vocabulary guide** — class or element → when to use it — so the served file is simultaneously the styling and its own documentation. One artifact, nothing to drift against. This alone fixes the 147 existing section-docs.

### 2. Serve the contract through the existing discovery channel

Skills already fetch the manifest endpoint (`GET /api/manifests/{doc_type}`) to learn section keys. That is the natural place to point them at the presentation contract — the commented `doc.css` itself, served at a **stable (non-cache-busted) URL** so skills always fetch current content — and to **advertise the `extra_css` affordance**: "here is the vocabulary; if you need more, send scoped extra_css and it will be flagged for review."

### 3. Scoped `extra_css` escape hatch — not a strip

Documents may submit an optional `extra_css` field, stored with the content version and rendered **only in the native/full render of the document it belongs to** — not in the diff view (structural) or the synthesis-native view (a different doc type). All respected author CSS is **confined to the document body** so it cannot reach the shell chrome — preserving the exact reason the original strip existed — and it must **take precedence over the canonical rules**, since overriding a default is the whole point of an escape hatch.

Opaque docs' own `<style>` is *scope-and-kept* rather than dropped: author style content is gathered and confined to the document body; rules that cannot be scoped (e.g. document-level at-rules such as `@import`) are dropped rather than allowed to bleed. This is a deliberate, *scoped* reversal of the F2 strip decision (recorded in Design notes), not an undo of it.

### 4. Retro-driven promotion ratchet

Every `extra_css` submission is logged. The feature-retro skill is intended to surface it as "this doc needed bespoke CSS — promote into doc.css?". Novel content renders correctly immediately; recurring needs graduate into the canonical stylesheet deliberately. **This feature ships the logging only** — the surfacing lives in the retro skill (a separate repo) and is deliberately deferred; see Design notes.

## Testing

The riskiest change is reversing the strip in the render chokepoint, so the contract to prove is mostly about *containment and exactness*:

- **No chrome bleed.** Scoped author/extra CSS must not affect the shell chrome — breadcrumbs, comment rail, sticky action bar — even when it targets elements those share. This is the regression the original strip prevented.
- **Scope-and-keep recovers opaque `<style>` correctly.** Author style survives, renders within the body, and unscopable at-rules are dropped rather than leaked.
- **Usage event is exact.** Fires exactly once per write carrying non-empty `extra_css`, and not at all for absent/empty/whitespace-only.
- **Render-mode boundary.** `extra_css` applies in the native render only — never injected into diff or synthesis-native views.
- **Lightweight coverage guard (Phase 1).** A test asserts the canonical `doc.css` carries rules for the curated set of classes/elements we widen for, so a future deletion regresses loudly. Broad scanning of every tag/class the live corpus emits is deliberately *not* automated here — that long tail is left to the human retro ratchet.

## Alternatives

1. A separate vocabulary/contract file (JSON catalogue of class names)
  discussed with user; recorded in the captured context
  Rejected. A second artifact describing the stylesheet is a
  second source of truth that will diverge from the CSS — reintroducing the exact drift
  that caused this bug. The commented stylesheet is its own contract. (A

  ,
  drift-free index remains a fallback if raw CSS proves a poor contract format for skills —
  see Design notes.)
2. Keep stripping all author CSS; never widen
  the status quo
  Rejected. Leaves novel content permanently unstyleable and
  forces skills to either hallucinate unknown classes (today's silent breakage) or
  refuse to render. The escape hatch exists precisely so the answer to "the contract
  doesn't cover this" is never "render it broken".
3. Per-doc-type stylesheets
  considered during requirements
  Rejected. Most of the vocabulary (tables, headings,
  blockquote, lists) is shared across doc types; splitting multiplies the surface and
  the drift risk for little gain.

## Delivery phases

### Phase 1 — Widen & document canonical doc.css

Add rules for the corpus vocabulary inventoried in *Problem*, each block commented as a vocabulary guide, plus the lightweight coverage test from *Testing*. Pure CSS plus one test — no schema, no API change. This resolves the reported bug and fixes all 147 existing section-docs immediately. Lowest risk, highest immediate value; ships first and alone.

### Phase 2 — Serve & advertise the contract

Expose the commented `doc.css` to skills as the discoverable contract — served at a stable URL with the manifest endpoint pointing at it — and advertise the `extra_css` affordance in the contract text. Read-only; no stored state yet.

### Phase 3 — Scoped extra_css escape hatch + usage logging

Accept the optional top-level `extra_css` on document write, store it with the content version, and render it scoped to the document body (native render only, with precedence over canonical rules). Scope-and-keep opaque docs' own `<style>`, which recovers the 7 affected docs **best-effort** — no dedicated backfill. Log each non-empty usage as an event. Highest blast radius — it touches the write path, the render path, and reverses the F2 strip decision in a scoped way — so it ships last, after the contract is in place for skills to lean on.

*Surfacing logged usage in feature-retro is downstream (the retro skill, a separate repo) and not a phase of this feature.*

## Indicative notes

Plan-level leanings carried forward from the captured context's open questions and review round 1 — not requirements, but the starting point for the plan:

- **Containment selector + mechanism:** confine respected CSS to the document body (`main.document` / `#doc-main`). CSS `@scope { … }` is clean given a single known browser on localhost; selector-prefixing is more portable but needs a CSS parse/rewrite step. Lean `@scope`; the plan should confirm browser support and any validation burden.
- **Cascade / precedence:** make `extra_css` reliably override canonical rules with `@layer canonical, extra` rather than relying on selector specificity. Containment and cascade are separate problems.
- **extra_css storage mechanism:** the per-version `content_json` blob is the natural home — it versions with content automatically and needs no migration. (The field shape is pinned in *Data model*; this is the storage mechanism.)
- **Usage logging:** an `events` row keyed to `document_id` (e.g. an `extra_css_used` event) reusing the existing events stream the inbox already consumes.
- **Contract serving:** serve the raw commented `doc.css` at a stable URL and have the manifest endpoint return a pointer to it. Avoid a hand-maintained derived index — that is the drift to avoid; a *generated* index stays drift-free if raw CSS later proves insufficient for skills.
- **Backfill of the 7 opaque docs:** best-effort. Check whether they render acceptably from the widened canonical CSS before any per-doc work; scope-and-keep recovers the rest as a side effect.
- **How much to widen now:** cover what the corpus measurably uses today; lean on the extra_css hatch for the long tail rather than speculatively styling vocabulary no document emits.

## Design notes

Decisions captured from requirements review round 1:

- **extra_css shape:** an optional top-level field on the document write, stored within the content version (R1, confirmed).
- **F2 strip reversal:** the `<style>` strip — a stated requirement of *server-rendered-docs* (F2) — is deliberately and partially reversed here via scoped scope-and-keep. Recorded here as the decision trail; F2's own doc is intentionally left untouched (R1).
- **extra_css is rare by design:** a promotion-bound escape hatch, not a routine per-doc styling channel (R1).
- **Ratchet ships write-only:** usage logging is in scope; surfacing in feature-retro is deliberately deferred to the cross-repo skill. Accepted that events may accrue unread until that work lands (R1).
- **7 opaque docs:** best-effort recovery only, via scope-and-keep — no dedicated backfill job (R1).
- **Accepted trade-off:** the commented stylesheet is the contract even though raw CSS is a harder format for an LLM to consume than a structured list. Mitigated by writing the comments as an explicit vocabulary guide; a generated (never hand-maintained) index is the fallback if skill-side consumption proves it insufficient (R1).
