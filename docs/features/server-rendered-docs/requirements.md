# server-rendered-docs

## Problem

Today a doc is rendered as a thin shell that iframes the doc's standalone HTML file. `doc_shell` serves `doc.html`, which embeds `<iframe src="/doc/{id}/raw">`; `doc_raw` serves the doc's own self-contained HTML live from `source_path` (preferring `content_html` if present, currently always NULL).

Because the rendered doc is an opaque file inside a frame, the two interactive features have to reach *into* that frame to work. Synthesis reads `.your-thoughts textarea` and `.tier-routine .flag-btn.active` out of `frame.contentDocument`; comments read `window.__fsComments` out of `frame.contentWindow` — both via inline scripts in `doc.html`, POSTing to `/doc/{id}/synthesis-response` and `/doc/{id}/comments`.

That cross-repo DOM-scraping is brittle by construction: it only works while the feature-skills templates keep exactly the structure the scraper expects, and a template tweak in the other repo can silently break the handoff. `skill-webapp-integration` hit exactly this class of bug — unexpanded `~`/`$HOME` paths silently 404'd the entire HTTP handoff, with no loud failure. It's the fragility the structured-content decision was chosen to retire.

The presentation layer compounds it. Each feature-skills template carries its own copy of the chrome — TOC, syntax highlighting, the comment-rail widget, the synthesis response widgets, the sticky footer CTA. There's no single owner, so it drifts; and it can't be shared for the emerging cross-agent goal (running the same workflow from Codex as well as Claude) without each agent's skills carrying — and diverging — their own copy.

Now that `versioned-content-store` (F1) has shipped, each doc's content lives in the DB as structured, versioned content reachable via `current_content(conn, doc_id)`. The webapp can finally render the doc itself instead of iframing a file — and the scraping can stop.

## Vision

The webapp renders every doc from its own stored content through one webapp-owned presentation layer, and comments and synthesis are first-class native UI — replacing a silent cross-repo DOM scrape with a tested, same-repo render that fails loudly.

## User stories

1. As Nigel reviewing a requirements or plan doc
  I want the comment rail to be native page UI, not scraped out of a frame
  I select a passage and leave a comment; a later tweak to a feature-skills template can't silently break comment submission, because the webapp owns the widget and reads from its own DOM.
2. As Nigel answering a synthesis (feedback) doc
  I want the per-item response widgets rendered by the webapp
  I fill in my thoughts per item and flag a couple of routine items; submitting posts the same

  shape as today, but no longer depends on the feedback file's internal DOM matching what a scraper expects.
3. As Nigel returning to a doc I've already responded to
  I want my previously-submitted answers and comments shown
  I reopen a feedback or requirements doc I answered earlier and see my responses/comments already in the widgets, so I can revise rather than start from a blank slate.
4. As Nigel — and a future Codex wrapper
  I want the section manifest and presentation owned once, by the webapp
  An agent fills in sections by logical key without embedding any knowledge of how a doc looks; the rendering and section ordering live in the webapp, so neither Claude's nor Codex's skills carry their own drifting copy.
5. As Nigel using any doc with a keyboard or screen reader
  I want the native widgets to be keyboard-operable and labelled
  Since the comment and synthesis widgets are being rewritten as webapp-owned UI, they get proper labels and focus handling now, rather than inheriting the iframe's stray-aria-label state.
6. As Nigel opening a doc that pre-dates F1 or failed to import
  I want a graceful fallback, not a blank page
  A doc with no stored version still shows me something useful — the raw source rendering with a visible note — rather than an empty shell.

## Data model

**No new tables.** F2 is a read-model + presentation change over what F1 and earlier features already store:

- **Reads** structured content from `document_versions` (via the existing `current_content()` accessor) — the seam F1 deliberately left for F2 to consume.
- **Keeps writing** the existing `comments` and `synthesis_responses` tables through the existing doc-id-keyed POST endpoints; the contracts are unchanged. For prefill, F2 also *reads* those tables by `document_id` at render time (the shell already has the id).
- **Extends** the per-type manifests in `doc_content.py` with an ordered `(key, label)` sequence — today they carry section *keys* only, and rendering needs ordering plus a heading/TOC label. This becomes the single render source of truth.
- `content_html` stays NULL — render on the fly from the current version. It remains a render-cache seam to populate later only if render cost ever matters.

For the section-parsed types (context, requirements, plan), each stored section body already includes its own `<h2>` and inner markup, so rendering is mostly emitting bodies in manifest order. For the *opaque* types (feedback and the tracker), the stored body is the **entire source file** — `<head>`, `<style>`, `<script>` and all — not just the document body, which shapes how they must be rendered (see Technical approach).

## Technical approach

### Render server-side, drop the iframe

`doc_shell` stops emitting `<iframe src="/doc/{id}/raw">` and instead renders the stored content inline into `<main class="document">`, wrapped in a webapp-owned page: the TOC + scroll-spy, syntax highlighting, breadcrumbs, sibling nav, and the missing/archived/503 states that `doc-view` already established. The page extends the pattern `feature_page.py` and `project_page.py` already use — server-rendered from the DB, no frame.

### Trust model and a single render-safe extractor

The iframe was an implicit trust boundary: scripts and styles in the doc file couldn't touch the webapp shell. Rendering stored HTML directly into the page removes that boundary, so the trust model must be explicit: **stored content is trusted but rendered safely** — the webapp strips document furniture (`<head>`, `<style>`, `<script>`) and emits only inner content. This matters most for the opaque types, where the stored body is a whole file with its own styles (sharing CSS-variable names like `--bg`/`--accent`) and inline scripts that would otherwise apply/execute in the webapp's origin next to the submit logic. A single server-side extractor/sanitiser is the one chokepoint for this — serving native rendering, the raw hatch, and (later) the agent write path uniformly. This is the natural home for "own the presentation once".

### The presentation layer relocates into the webapp, owned once

The CSS/JS that each feature-skills template carries today becomes the webapp's, applied to every doc. The webapp's presentation JS (comment rail, synthesis widgets) lives as a **tested webapp-owned static asset**, not re-pasted inline script — otherwise the inline-copy problem just moves and the "owned once, no drift" goal isn't actually met. Trimming the now-unused blocks out of the feature-skills templates is a later, cross-repo cleanup — **out of scope here** (this feature is webapp-side only; skills still write files).

### Native comments (requirements & plan)

The click-to-comment trigger, popover and comment rail render as part of the webapp page and submit to the existing `POST /doc/{id}/comments` (payload `{comments: [{excerpt, text}]}`). The rail also displays any previously-submitted active comments on load (prefill). The frame-scraping comment JS in `doc.html` goes away — the webapp reads its own same-page DOM.

### Native synthesis (feedback) — rendered, not inlined

Feedback docs are stored opaque (the whole file). Because inlining that file would inject a foreign document into the page, the webapp **parses the stored body to extract the item structure** (tier, item number, the "my take" text, the response/flag affordances) and renders its *own* native per-item widgets, discarding the file's furniture. The submit handler builds the existing `{responses, routine_flags}` shape and posts to the unchanged `POST /doc/{id}/synthesis-response`; widgets prefill from any existing responses. Nothing about the feedback structure is lost. This relocates the parsing coupling from JS-in-iframe to Python-at-render — it doesn't eliminate the coupling, but it contains it: same repo, unit-testable, and it fails loudly rather than silently 404ing.

### Manifest ownership (exposure deferred to F3)

F2 makes the webapp the single owner of the section manifest (the extended `(key, label)` structure above) and uses it to render. *Exposing* that manifest to external agents — so a Codex wrapper can fill sections without embedding presentation knowledge — is deferred to `agent-submission-api` (F3); F2 needs the manifest only internally. F3's context doc is updated to record that manifest exposure is its responsibility.

### Raw hatch retained

`/doc/{id}/raw` stays as a "View source" escape hatch — useful when a native render looks wrong — pointed at the original file/content. It stops being the primary render path and becomes a debugging affordance only.

### Tracker surfaces left as they are

The project and feature pages already render server-side from the `features` table, not from doc content — they stay. Only the per-doc *view* of the tracker document itself is rendered natively (its inner content extracted from the opaque body); F2 does not re-architect the project page.

### Fallback for unversioned docs

A doc with no version row (couldn't be parsed, or pre-dates F1) falls back to the raw source-file rendering — the current behaviour — with a visible note, rather than a blank page. F1's importer runs on every walk, so this should be a safety net rather than the expected path (see Design notes for the open sub-question on guaranteed corpus coverage).

### Live updates out of scope

The POST handlers call `broadcaster.broadcast()`, but the native doc page does not subscribe to `/events` in F2 — live-refresh of an open doc view is out of scope here.

### One switch, not a permanent halfway house

All doc types move to native rendering; the design stance is not to maintain old-and-new in parallel long-term. The phasing below lands across two MRs; the only transient is feedback briefly still framed between them, and Phase 2 must follow Phase 1 before the branch is considered done so that transient never persists.

## Alternatives considered

1. Inline the opaque feedback body and bind scraping JS to same-page DOM ("minimal")
  Source: derived from the current doc.html scraper; weighed in round 1
  Rejected. The opaque body is a whole HTML file, so
        inlining it injects a nested document — its

  clobbers
        the shell theme (shared variable names) and its inline scripts execute in the
        parent page. "Minimal" therefore isn't cheap: it requires stripping head/style/
        script first, which is most of the work of rendering properly. So we render,
        not inline.
2. Switch only read-only docs to native, keep the iframe for interactive ones
  Source: design stance in the context doc
  Rejected: contradicts the "all doc types switch
        together / no halfway house" decision. Doing them all at once is what surfaces
        issues across the doc types.
3. Re-import feedback as structured sections in F1
  Source: F1 storage design
  Rejected for F2: F1 shipped feedback deliberately
        opaque. Re-structuring storage is a bigger change than parsing the opaque body
        at render time, and re-opens F1's importer.
4. Populate content_html as a render cache now
  Source: F1 left content_html NULL as a seam
  Deferred: render-on-the-fly is simpler and correctness
        is easier to reason about. content_html remains available as a cache later if
        render cost is ever a problem.

## Delivery phases

### Phase 1 — Native render engine + presentation + native comments

Server-render context, requirements, plan and the tracker doc from stored content into the webapp-owned shell (via the single render-safe extractor); drop the iframe for these. Preserve breadcrumbs, sibling nav, the view-source hatch, and the missing/archived/503 states. Manifest-driven section ordering, TOC scroll-spy, syntax highlighting. Wire the native comment rail for requirements/plan (posting to the existing endpoint, prefilling existing comments) so their comment flow doesn't regress when the frame goes. Fall back to raw rendering for any doc with no stored version. Webapp JS lands as a tested static asset. Delivers: every non-feedback doc reads and comments natively, no scraping.

### Phase 2 — Native synthesis for feedback docs

Render feedback docs natively by extracting item structure from the opaque body and rendering webapp-owned per-item widgets, prefilled from existing responses and posting the existing `{responses, routine_flags}` shape; drop the iframe and frame-scraping for feedback. Retire `doc.html`'s scraping scripts; `doc_raw` survives only as the raw hatch. Must follow Phase 1 within the same branch/work cycle (no lasting halfway house). Delivers: the whole corpus is iframe-free and interaction is first-class.

## Indicative implementation notes

Plan-level detail to carry into `/feature-plan`; not binding:

- `current_content(conn, doc_id)` returns a `ParsedContent`; for section types, render in stored order, wrapping each `Section.body` in `<section id="{key}">` inside `<main class="document">`.
- Manifests in `doc_content.py` gain an ordered `(key, label)` structure (today `expected_keys` only) as the render/TOC source.
- A single server-side extractor takes any stored body (section or opaque) and yields render-safe inner HTML — strips `<head>`/`<style>`/`<script>` and document furniture. Used by native render, the raw hatch, and the future write path. F1's `_SectionParser` is section-specific, so feedback extraction is a separate (likely `html.parser`-based) pass for `.item[data-item]`, tier class (`tier-needs-input`/`tier-feedback`/`tier-routine`), `.item-num`, the "my take" text.
- Feedback submit handler mirrors today's `doc.html` synthesis script (collect by `data-item` → `{responses, routine_flags}`); comment payload stays `{comments:[{excerpt,text}]}`.
- Prefill reads are doc-id-based (the shell has `document_id`), sidestepping the `source_path`-keyed `GET` accessors the skill polling uses.
- `doc.html` becomes the full server-rendered page rather than a frame shell; `doc_raw` stays only for `/raw`.
- Tests to cover: per-doc-type render snapshot; comment + synthesis POST integration against the rendered widgets; prefill of existing responses/comments; fallback-to-raw when no version row; the 404 / missing / archived / 503 states; tracker-doc opaque render; the extractor stripping furniture/scripts.

## Design notes

- **Synthesis renders, not inlines (round 1).** The opaque feedback/tracker body is the whole source file, so the "minimal inline" option would inject a foreign document (style clobber + script execution). Deep render — parse the body, render webapp-owned widgets — is therefore the default, not a purity upgrade.
- **Prefill in scope (round 1).** Rendering server-side with the `document_id` in hand makes showing previously-submitted responses and comments cheap; brought into scope as a resumable edit surface.
- **Manifest exposure is F3's job (round 1).** F2 owns the manifest internally and renders from it; exposing it to external agents is deferred to `agent-submission-api`, and that feature's context doc is updated to record the responsibility.
- **Explicit trust model (round 1).** Dropping the iframe removes a boundary, so a single render-safe extractor that strips head/style/script is a stated requirement, not an implementation detail.
- **Honest framing (round 1).** Native rendering relocates and contains the template coupling (Python-at-render, tested, fails loudly) rather than eliminating it.
- **Open sub-question (round 1).** Whether F1's importer is guaranteed to have covered the whole corpus before F2 ships, or whether the raw-render fallback is load-bearing — to confirm during planning.
