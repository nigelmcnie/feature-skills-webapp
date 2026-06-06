# doc-view

## Problem

`inbox-view` shipped the headline UX — a cross-project inbox of cards for docs with new activity, features in progress, and recently shipped work. But the cards are **display-only**: clicking one does nothing. There is still no way to open and read a doc *inside* the webapp. To actually read a doc you fall back to opening the dev-store `.html` file directly in Chrome — the exact focus-stealing behaviour the webapp exists to eliminate (design doc §1, §2).

Two upstream features deliberately left their last mile to this one:

- `inbox-view`'s requirements state plainly: *"Clicking a doc in place — rendering it inside the webapp shell with breadcrumbs and stamping `read_state.last_read_at` — is `doc-view`'s job. `inbox-view` renders the card's identity and last activity but does not own a doc route."*
- `read-state` shipped a fully-tested `mark_read(conn, document_id)` operation but nothing calls it at view time: *"the actual render-time call is wired in when `doc-view` lands."*

The consequence: the **"New since last visit"** category never clears, because nothing ever marks a doc read. The webapp's core loop — see a card, open it, it leaves the inbox — is broken at the "open it" step.

## Vision

Clicking an inbox card opens the doc inside the one always-open webapp tab — wrapped in chrome with breadcrumbs and sibling-doc navigation — and the act of viewing marks it read, so it drops out of "New since last visit" on the next inbox load.

## User stories

1. As Nigel, reading the inbox
  I want to click a card and read the doc in the same tab
  A fresh

  doc
        appears under "New since last visit". I click the card and the doc opens
        in place — no new Chrome window grabs focus mid-thought.
2. As Nigel, working through the inbox
  I want viewing a doc to mark it read automatically
  After reading that feedback doc I navigate back to
        the inbox. It's gone from "New since last visit" — I don't have to
        remember what I've already looked at.
3. As Nigel, reading a rendered doc
  I want it to render with its own styles and interactivity intact
  A requirements doc opens with its table of contents,
        syntax-highlighted code blocks, and the click-to-comment widget all
        working — the same content I'd see opening the file directly, now framed by
        the webapp shell.
4. As Nigel, oriented in a feature
  I want to know which doc I'm in and step between a feature's docs
  Reading

  's plan, I see

  breadcrumbs and can
        jump straight to its requirements without bouncing back to the inbox.

## Data model

No new tables and **no migration**. `doc-view` is a reader over the existing schema plus one write that already exists:

- **Reads** `documents` for the doc's type, status, and `source_path`, joined to `projects` and *left*-joined to `features` (the project-level tracker doc has no feature) for the breadcrumb identity.
- **Writes** `read_state` via the existing `mark_read(conn, document_id)` from `read-state` — the render-time stamp those requirements anticipated.
- **Doc content, Stage 1:** the canonical bytes are the file at `documents.source_path` on disk. `content_html` stays `NULL` until Stage 2 makes SQLite the content store; if it is ever populated, the content endpoint should prefer it over the file.
- **A feature's doc set** (for sibling navigation) is the `documents` rows sharing a `feature_id`.

## Technical approach

### Address docs by document id

The shell is addressed by the document's primary key. The inbox card already carries `document_id`, so no new data has to be threaded through the inbox read model; the key is unambiguous; and it sidesteps route collisions with `/healthz`, `/admin/…`, `/static`, and the `feature`-less tracker doc. A document keeps its id across re-walks (the walker upserts by `source_path`), so a bookmarked doc URL survives re-renders — though a file *move* mints a new id, since the old path goes `missing` and the new path is a fresh row. Human- readable `/<project>/<feature>/<doc-type>` URLs (the design doc's sketch) are noted under Alternatives as a Stage-2 nicety — breadcrumbs still *display* that readable identity even though it isn't in the URL.

### Render by passing the file through an iframe

The shell is a thin, webapp-owned page: a top bar with breadcrumbs (and, in phase 2, sibling nav) above an `<iframe>` that fills the rest of the tab and points at a content endpoint, separate from the shell, that streams the indexed file's bytes verbatim. The iframe scrolls internally — we accept the nested-scroll model (and the doc's own sticky TOC/footer being keyed to the frame) rather than auto-resizing to content height, which would mean a cross-repo `postMessage` change to every `feature-skills` template (see Design notes and Alternatives). This honours the Stage-1 design note — *"pass through the existing CSS embedded in the source files; don't try to restyle"* — and, critically, keeps each doc's embedded JavaScript working untouched: the TOC scroll-spy, syntax highlighting, and the click-to-comment widget. That last one matters because `comment-capture` (a later feature) builds directly on it. Inlining the doc body into the shell template would force re-wiring all that JS and risk CSS collisions; we explicitly do *not* do that in Stage 1 (it's the design doc's Stage-2 plan).

**A known downstream seam:** the file served into the iframe has no knowledge of its own `document_id` (its embedded `docId` is a path string). When `comment-capture` later switches the widget from clipboard to `POST`-ing comments, the content endpoint will need to inject the real `document_id` into the served document. Out of scope here, but flagged so that feature inherits it rather than rediscovering it.

### Serve content only for indexed docs

The content endpoint looks the document up by id and reads its recorded `source_path`; it never accepts a caller-supplied path. The walker already constrained `source_path` to within the docs root, so this keeps file-serving inside a trust boundary. The webapp is loopback-only, but we still don't want to open a path-traversal surface — serving arbitrary filesystem paths is out of scope. Both `active` and `archived` docs may be served (see "Reaching docs the inbox doesn't surface").

### Stamp read-state on render, exactly once per view

"Render" means the shell GET succeeded; that request stamps `last_read_at` via `mark_read`, per `read-state`'s design ("stamp on page render, not page close"). Only the shell request stamps — the iframe's content fetch does not — so one human view equals one stamp, even if the iframe never finishes loading. Because the stamp's timestamp (from `now_iso()`) is later than the doc's newest event, the doc drops out of "New since last visit" on the next inbox load. If a discovery walk happens to land a *new* event microseconds after the stamp, the doc simply re-appears as new next time — that's correct behaviour, not a race to defend against.

### Wire inbox cards to the doc, accessibly

`inbox-view` left cards display-only; `doc-view` makes a card a link to its doc. Cards that carry no document — the "In progress" and "Recently shipped" cards, which summarise a feature rather than a specific doc — are not links. The linked card must have a sensible accessible name (not just a run-together string of its inner spans) and a visible focus style for keyboard users. Beyond that, accessibility stays minimal — this is a single-user local tool, not a public site.

### Reaching docs the inbox doesn't surface

Two doc kinds never appear as inbox cards: the project-level tracker (`feature_id IS NULL`) and archived feedback docs (`status='archived'`). doc-view *renders* both correctly if navigated to — the tracker's breadcrumb reads `project / Tracker`, an archived doc's breadcrumb marks it archived — but adds *no* new entry point to reach them. They're reachable by direct/bookmarked URL (and, for archived docs, potentially via phase-2 sibling nav). A first-class "Tracker" affordance is deferred to a later project-view feature; this keeps the shell route robust without creeping into inbox territory now.

### Degrade gracefully

Unknown id → 404. A doc whose underlying file has gone missing (walker status `missing`, or an unreadable file) → a small "no longer available" page rather than a stack trace. (The shell stamps read-state before the content fetch discovers the file is gone — acceptable, and moot in practice since missing docs are already filtered out of the inbox.) DB not configured → 503, matching the existing route conventions in `routes.py`. The shell also offers a **"view source file"** link to the raw content — an escape hatch that serves the design's graceful-degradation principle and hedges against the iframe's rough edges.

## Alternatives considered

1. Human-readable URLs
  Source: design doc §6 (doc-view feature card)
  The design doc sketches this URL shape. Deferred
        to Stage 2 rather than chosen for Stage 1 because: (a) the inbox card
        carries

  but not the raw doc-type, so readable URLs
        would mean threading extra data through

  's read
        model; (b)

  is not guaranteed unique —
        an active doc and an archived feedback round can share a type — so it needs
        tie-break rules; (c) it collides with literal routes and the

  -less tracker doc, needing careful route ordering.

  is simpler and already in hand; breadcrumbs still
        display the readable identity. Confirmed in round 1.
2. Auto-resize the iframe to content height (postMessage)
  Source: review round 1
  Would eliminate the nested-scroll experience and
        the doc's sticky TOC/footer being keyed to the iframe box. Not chosen for
        Stage 1: the docs don't emit height messages today, so it needs a
        cross-repo change to every

  template. With the
        whole concept likely to be iterated soon, the simple internal-scroll iframe
        is the right Stage-1 investment.
3. Inline the doc body into a webapp template
  Source: design doc §6 ("Stage 2: pull just the body, render via the webapp's own templates")
  Gives the webapp full control of chrome and lets
        docs share one stylesheet. Not chosen for Stage 1 — the design doc itself
        files it under Stage 2. It requires extracting body + styles, re-wiring the
        embedded click-to-comment / TOC / highlight JS, and resolving CSS conflicts
        with the shell. Iframe passthrough defers all of that until the content
        actually lives in SQLite.
4. Serve files via a StaticFiles mount over the docs root
  Source: obvious framework pattern
  Cheapest possible file-serving, but it bypasses
        read-state stamping, can't wrap chrome or breadcrumbs, and would expose the
        whole docs tree rather than only indexed docs. Every view needs to go
        through a route that stamps and resolves identity, so a static mount is the
        wrong tool.

## Delivery phases

### Phase 1 — Open and read a doc, marked read

The shell route (breadcrumbs from project / feature / doc type, including the feature-less tracker and archived-doc cases), the content endpoint feeding the iframe, read-state stamping on render, a "view source file" escape hatch, and the not-configured / unknown / missing states. Inbox cards that carry a document become accessible links to it. Delivers the headline loop end to end: click a card → read it in the same tab → it leaves "New since last visit".

### Phase 2 — Sibling-doc navigation

Prev / next links across a feature's docs in canonical lifecycle order (context → requirements → plan → review), surfaced in the shell's top bar, reusing the existing doc-type ordering rather than introducing a second source of truth. Delivers moving between a feature's docs without bouncing back through the inbox. Separable and independently testable, so it ships as its own MR.

## Indicative implementation notes

Plan-level detail carried forward for `/feature-plan`; not binding.

- Likely a new `web/doc_view.py` (route handlers) plus a `doc.html` shell template, mirroring the existing `routes.py` / `index.html` split. Register the routes in `web/app.py`. Concrete route shapes (subject to the plan): `/doc/<id>` for the shell and `/doc/<id>/raw` for the content endpoint.
- Shell handler: one lookup joining `documents` → `projects` and `LEFT JOIN features` (the tracker doc has no feature) by id; 404 if absent; render the shell; then call `mark_read(conn, id)`. Note `mark_read` opens its own `transaction()`, so call it after the read query, not nested inside another transaction. Use the existing `request_conn(app)` per-request connection helper.
- Content endpoint: resolve `source_path` by id, require status `active` / `archived` and a non-NULL path, read the file, return an `HTMLResponse` of its bytes; unreadable / missing file → 404. Forward note: prefer `content_html` if it's ever populated (Stage 2). The served file currently can't know its own `document_id` — `comment-capture` will need the endpoint to inject it.
- iframe attributes need thought: the docs pull CDN assets (highlight.js), and the click-to-comment JS must run, so any `sandbox` / CSP must allow same-origin scripts and the CDN — or omit sandboxing given it's our own loopback-served content. The iframe fills the available height and scrolls internally. Decide specifics in the plan.
- `InboxCard` already has `document_id`; the `index.html` change wraps the card in an anchor to the doc when `document_id` is not None, with an accessible name and a `:focus-visible` style.
- Read-stamp correctness rides on `now_iso()`: the stamp is lexically `>` the doc's newest `events.created_at`, and the unread predicate uses strict `>`, so a viewed doc reliably reads as read next time.
- Phase 2 sibling order: rank documents of the same `feature_id` by a canonical type order (context < requirements < plan < review), reusing/centralising the ordering concept that `inbox.py`'s `humanise_type` / `_TYPE_LABELS` already implies; keep nav on the active narrative spine — exclude archived feedback rounds and missing docs.
- Breadcrumb cases: `project / feature / Type` for a normal doc; `project / Tracker` for the feature-less tracker; mark archived docs as archived in the breadcrumb.

## Design notes

- **iframe scroll model (round 1):** chose a viewport-filling iframe that scrolls internally over postMessage auto-resize. Simplest for Stage 1, no cross-repo change, and the whole concept is likely to be iterated soon — so the "identical to Chrome" framing was softened to "its own styles and interactivity intact, framed by the shell."
- **document_id routing (round 1):** confirmed over the design doc's human-readable URLs for Stage 1; readable URLs deferred to Stage 2.
- **Tracker & archived docs (round 1):** rendered correctly if navigated to, but no new entry point added — a first-class Tracker affordance is deferred to a later project-view feature.
- **comment-capture seam (round 1):** recorded that the content endpoint will need to inject `document_id` into the served file when comment-capture lands.
- **View-source escape hatch (round 1):** added to phase 1 as a graceful-degradation hedge.

## Review decisions

### Round 1 (post-merge review)

Reviewer verdict: faithful, complete, no blockers. Three cleanups applied directly to main:

- **Redundant feature-id lookup:** `doc_shell` re-queried the feature id by `(project name, slug)` before `siblings()`, despite already joining `features`. Folded `f.id AS feature_id` into `ROW_SQL` and pass it straight through — one fewer round-trip per feature-doc view.
- **Untested cache header:** added a test asserting `Cache-Control: no-store` on the inbox response — the load-bearing half of the back-button stale-read-state fix, previously unguarded against a refactor.
- **Duplicated "Tracker" literal:** the feature-less breadcrumb now routes through `humanise_type("features")` rather than hardcoding the string, keeping one source of truth.
