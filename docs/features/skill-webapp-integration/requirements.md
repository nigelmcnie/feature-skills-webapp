# skill-webapp-integration

## Problem

The webapp now does almost everything the `feature-skills` review loop reaches for the browser and the clipboard to do — but the skills don't use it yet. The whole point of building the webapp was to retire those round-trips, and this is the feature that finally does it.

Two habits remain wired into the skills:

- **Five `google-chrome … &` auto-opens** scattered across `feature-context`, `feature-requirements` (×2), `feature-plan` and `feature-review` pop a fresh browser tab every time a doc is written. The inbox already surfaces exactly these docs — "New since last visit", live over SSE — so the auto-open is redundant tab-spam that competes with the inbox as the place to look.
- **Every review round hands feedback back through the clipboard.** The developer clicks *Copy responses* (synthesis answers) or *Copy comments* (click-to-comment marginalia on `requirements.html` / `plan.html`), alt-tabs to the terminal, and pastes a JSON blob into the chat. This is the same fragile path `synthesis-response-capture` set out to kill — one clipboard slot, silently lost if anything else is copied in between, no server-side record — yet the skills still depend on it.

Concretely, the pieces are half-built:

- The synthesis-response endpoint *already exists* (shipped in `synthesis-response-capture`: `GET /synthesis-response?path=…`, with a `submitted` flag) but **nothing reads it**. That feature explicitly left the clipboard "as a fallback until skill-integration-parallel". This is that feature.
- Comments have **no webapp path at all**. `comment-capture` (#8 in the build order) was going to persist click-to-comment annotations server-side, but it hasn't been built — so the only way a comment reaches the agent is still the clipboard.
- Because the handoff is a paste, the agent **blocks**: it presents a doc and waits for the developer to read, answer, copy, alt-tab and paste. The review is serialised through the terminal even though the developer is already in the browser, where the doc is rendered and the inbox is live.

## Vision

A developer reviews a feature doc entirely in the webapp — finding it in the inbox, answering synthesis questions and leaving comments in place — and the agent picks the feedback up over HTTP on its own, with no browser tab thrown at them and no clipboard paste.

## User stories

1. As a developer reviewing a draft
  I want a new doc to appear in my inbox rather than a
        browser tab being opened at me
  I'm mid-task when the agent finishes a
        requirements draft. Instead of a Chrome tab stealing focus, the doc
        simply appears at the top of my inbox (live, via SSE), and I go to it
        when I'm ready.
2. As a developer answering a synthesis doc
  I want my submitted responses to reach the agent
        without copying a blob and pasting it into the terminal
  I fill in the feedback form in the webapp and
        click submit. The agent — which has been polling — picks the responses
        up and starts integrating. I never leave the browser.
3. As a developer leaving comments on a draft
  I want my click-to-comment marginalia saved in the
        webapp and read back by the agent
  I select a passage in

  ,
        leave a note, and submit. The comments are stored server-side; the
        agent reads them by the doc's path — and a stray clipboard copy can't
        lose them.
4. As a developer pulled away mid-review
  I want the feedback I already submitted to survive
  I answered a synthesis doc and closed the tab
        before telling the agent anything. The responses I submitted are
        already server-side — nothing hinged on a single clipboard slot or a
        chat paste I never made.
5. As the agent running a review skill
  I want to detect the developer's submission by
        reading an endpoint rather than blocking on a paste
  I present the doc, then poll the response and
        comment endpoints. When a submission lands I proceed — freeing the
        developer to stay in the browser and freeing me from a hard wait on a
        terminal paste.

## Data model

No migration. Like `synthesis-response-capture` before it, this feature is the *first writer* of a table that has existed, empty, since the very first migration — here, `comments`.

- **Comments.** One row per click-to-comment annotation, attached to the document it was left on — the main docs (`requirements.html` / `plan.html`), *not* the synthesis/feedback docs; click-to-comment lives on the spine docs. Each row carries the selected excerpt and the note text.
- **Submit replaces the active set.** A comment submit *replaces* the document's active (un-integrated) comment set with the rail's current contents — the same replace-on-submit discipline synthesis uses — while already-integrated comments are left untouched. So deleting a comment in the rail and re-submitting removes it from the active set; there is no separate delete endpoint.
- **Integration state.** A comment is *active* until the agent has folded it into a doc, then *integrated*. The agent marks integrated only the specific comments it just read (by id), never a blanket per-doc sweep — so a comment left *during* an integration round isn't silently retired unread. "Accrete across rounds" therefore means: integrated comments persist as history and drop out of the active read, and each new round the developer builds a fresh active set.
- **Retrieval identity.** Comments are read back by the document's source path — the dev-store path the skill already holds for a doc it generated — the same identity convention the synthesis read endpoint uses.
- **Audit.** Comment submit and integrate are logged to the append-only `events` table, consistent with how synthesis submit already records itself and drives the SSE broadcast.
- **Synthesis answers are unchanged.** They continue to hang off the feedback doc via the existing table and its replace-on-submit semantics. This feature only adds a *reader* on the skill side.

Relationships: one document has many comments; each comment is active or integrated. The skill side adds no schema — it is purely a consumer of the read endpoints.

## Technical approach

### Drop the Chrome opens — the inbox is the surface

Remove all five `google-chrome … &` invocations. The developer finds new docs in the inbox, which already lists "New since last visit" and updates live over SSE. The skills stop opening anything; they tell the developer the doc is ready and point at the inbox. We deliberately do *not* deep-link to a doc's webapp URL, because the skill holds the dev-store *path*, not the walker-assigned document id the webapp routes by, and resolving one to the other would add a lookup. Pointing at the inbox is enough and keeps the skills id-agnostic.

### Capture comments in the webapp by reading the iframe's own state

Give the webapp a comment-capture surface that parallels the synthesis one already shipped: a submit affordance on the `doc-view` shell, which POSTs to a write endpoint that persists into the `comments` table, and a companion read endpoint, keyed by the document's source path, returning the doc's active comments. There is *no* feature-skills template change.

Crucially, the shell does **not** scrape the rendered DOM for the comments. The spine template keeps its comments in a JavaScript array, and the rail only shows truncated, escaped projections of them — so the real payload isn't reconstructable from the DOM. Instead, because the doc renders in a same-origin, non-sandboxed iframe, the shell reads the iframe window's *live JS state* directly (the array the template already maintains). This is the honest version of "mirror synthesis with no template change": synthesis happens to expose its answers in stable DOM; comments are exposed in JS state; either way the same-origin shell can read what it needs without the template knowing about the webapp.

### Mark comments integrated so rounds don't re-serve them

Once the agent reads a doc's active comments and folds them in, it marks exactly those (by id) integrated, so the next round's read returns only new comments. This closes the "no consumed marker" gap that `synthesis-response-capture` explicitly left open — for comments, where it actually bites, since comments (unlike a wholesale-replaced synthesis submission) accumulate across rounds.

### Read feedback over HTTP, polling instead of blocking on a paste

The skills replace both clipboard handoffs with HTTP reads keyed by the dev-store path they already hold:

- **Synthesis answers** — read the existing `/synthesis-response` endpoint; its `submitted` flag is the definitive "the developer has answered" signal.
- **Comments** — read the new comment endpoint; the submit click is its terminal signal ("here is my current set").

Rather than blocking on a pasted blob, after writing a doc the skill **force-walks the indexer** (`POST /admin/discover`) and then **polls every 5 seconds**. The poll loop treats two states as distinct: a `404` means "not yet indexed" (keep waiting / re-trigger the walk), whereas a `200` with `submitted=false` / no comments means "indexed, awaiting the human". There is no hard timeout by default — a developer's review can legitimately take a long time — but the skill surfaces a periodic "still waiting on your review in the inbox" status so a long wait is visible. This is the "parallel" in the feature's lineage: the human's review loop no longer serialises through a terminal paste, and the reviewer subagent the skills already spawn keeps running alongside.

**Concurrency.** Polling means the developer and agent now operate on the same doc's comments at the same time — a surface the serial clipboard handoff never had. The read-then-mark-by-id contract above, plus the webapp's existing per-request-connection + WAL discipline, covers the read-during-submit window: the agent only marks what it actually read, so a comment submitted after that read stays active for the next round.

### The webapp is required infrastructure; clipboard stays as a graceful fallback

The README already pins `feature-skills` and the webapp as version-coupled, and the webapp runs as a supervised systemd service — so the skills can assume it's up. The clipboard-paste path is kept as a fallback for two cases: the endpoint is unreachable (`503` / connection refused), or the developer explicitly gives up waiting (the more likely escape hatch in practice). Stripping the clipboard buttons from the templates entirely is a larger, harder-to-reverse step, deferred.

### Trust boundary

No new boundary: the comment endpoints share the same loopback-only (`127.0.0.1`) boundary as the synthesis endpoints, and comment text is length-capped defensively exactly as synthesis responses are.

## Alternatives considered

1. Deep-link to the specific doc URL instead of the inbox
  Source: obvious alternative to "go to the inbox"; reviewed round 1
  The skill holds the dev-store path, not the
        document id the webapp routes by; deep-linking needs a path→id (or
        path→URL) resolver, so the headline flow points at the inbox and stays
        id-agnostic. Cheap to soften later: since the agent already calls the
        read endpoint, that response could

  return the doc's
        webapp URL at near-zero cost — noted as an optional addition, not a
        scoped deliverable.
2. bridge for comment submit
  Source: the original synthesis-response-capture requirements proposed a bridge
  Unnecessary. Because the iframe is same-origin
        and non-sandboxed, the shell can read the iframe window's comment state
        directly — simpler than a message protocol, and it works on
        already-generated docs with no template change. (Synthesis reached the
        same conclusion, reading the iframe's DOM directly.)
3. Scrape the rendered comment rail DOM
  Source: the literal "mirror synthesis (which reads DOM)" reading; raised round 1
  Doesn't work: the rail only holds truncated,
        HTML-escaped projections of each comment, so the real payload can't be
        reconstructed from it. Reading the iframe window's JS state avoids this
        entirely.
4. Build comment-capture (#8) as a separate feature first
  Source: the suggested build order; discussed with Nigel this session
  Folding the comment endpoint into this feature
        gives one coherent integration and avoids a skill-side change that
        could only retire one of the two clipboard paths. Chosen over the
        serial route.
5. Keep the agent blocking on a paste; endpoint as recovery only
  Source: the conservative handoff option offered this session
  Rejected by Nigel: non-blocking polling is the
        chosen handoff — it's the point of the "parallel" lineage and keeps the
        developer in the browser.
6. Expose a

  / count signal over SSE for the done-edge
  Source: reviewer's missed-opportunity, round 1
  Deferred. With submit as a clean terminal
        signal (replace-on-submit), the agent doesn't need to distinguish "no
        new comments" from "developer still typing" in v1. Worth revisiting if
        the submit edge proves insufficient.

## Delivery phases

Cross-repo — webapp phases land in `feature-skills-webapp`, skill phases in `feature-skills`. Ordered so endpoints exist before the skill side consumes them; the Chrome-drop (Phase 3) is independent and can ship at any point.

### Phase 1 — Webapp — capture & retrieve comments

The write endpoint (submit comments read from the iframe window's JS state via the `doc-view` shell, persist to `comments` with replace-active-set semantics, follow the established `503/404/400/200` admin-route contract, length-cap, log an `events` row and broadcast a `changed` event like synthesis submit), the `doc-view` comment-submit affordance, and the read endpoint keyed by source path returning a doc's active comments. Testable end-to-end over HTTP and by manual submit, independent of any skill change. This is `comment-capture` (#8) folded in.

### Phase 2 — Webapp — integration state for comments

Semantics to mark specific comments (by id) integrated, so a later round's read returns only new comments. Independently testable; could fold into Phase 1, kept separate because it's only exercised once a skill consumes comments (Phase 5).

### Phase 3 — Skills — retire the Chrome opens

Drop all five `google-chrome … &` invocations across `feature-context`, `feature-requirements`, `feature-plan` and `feature-review`; replace with a pointer to the inbox. No webapp dependency, smallest change, independently shippable.

### Phase 4 — Skills — read synthesis responses over HTTP

`feature-requirements`, `feature-review` and `feature-iterate` force-walk the indexer then poll the existing `/synthesis-response` endpoint (every 5 s), distinguishing the not-yet-indexed `404` from a `submitted=false` wait, and consume the returned responses / routine-flags instead of a pasted blob. Clipboard retained as the unreachable / give-up fallback. Depends only on the already-shipped synthesis endpoint.

### Phase 5 — Skills — read comments over HTTP

`feature-requirements`, `feature-plan`, `feature-review` and `feature-iterate` poll the comment read endpoint (same force-walk / 5 s / 404-vs-wait discipline) and mark the comments they fold in integrated (by id); clipboard retained as fallback. Depends on Phases 1–2. Completes the clipboard retirement.

## Indicative implementation notes

Plan-level detail surfaced during requirements exploration, carried forward for `/feature-plan`. Not binding.

- **Existing table.** `comments` is defined in `0001_init.sql`: `id`, `document_id` (`ON DELETE CASCADE`), `excerpt`, `text` (`NOT NULL`), `status` (`NOT NULL`), `created_at` (`NOT NULL`), `integrated_at`. No migration. Pick stable status values (e.g. `active` / `integrated`); replace-active-set on submit means deleting then re-inserting (or diffing) the doc's `active` rows in one transaction, leaving `integrated` rows alone.
- **Reading the iframe's comment state.** The spine template holds comments in a top-level `const comments = []` (see `requirements-template.html` / `plan`); the same-origin shell reads `iframe.contentWindow.comments` directly. Mirror `web/synthesis.py` conventions for the endpoint (`request_conn`, `transaction()`, `now_iso()`, `503/404/400/200`) and reuse `web/broadcaster.py` for the `changed` broadcast.
- **Comment payload shape** (from the spine template's "Copy comments" button — accept it verbatim for client reuse): The parent-frame document id is authoritative for routing; the body `doc` is at most validated, never trusted — same rule as synthesis.
  ```json
  { "doc": "docs/features/<FEATURE>/requirements",
    "comments": [ {"excerpt": "…", "text": "…"} ] }
  ```
- **Read endpoint** keyed by `documents.source_path`, returning active (un-integrated) comments with a clear "none yet" state. The skill holds the absolute path it wrote the file to, so it queries directly — no path→id normalisation needed. (Optionally also return the doc's webapp URL here — see Alternatives #1.)
- **Indexing.** Comments attach to `requirements.html` / `plan.html`, which already carry a `feature-doc-type` meta tag and are indexed by the walker — no new typing path needed (contrast synthesis docs). A doc must be indexed before its read endpoint returns non-404, hence the force-walk-then-poll, applied to *both* the synthesis and comment reads.
- **Skill-side polling.** After presenting a doc, force a walk (`POST /admin/discover`) then poll on a fixed 5 s interval (e.g. a bash poll loop); 404 = not yet indexed, 200+unsubmitted = awaiting the human. No hard ceiling by default; emit a periodic "still waiting" status; on an explicit give-up or an unreachable server, fall back to the clipboard paste.
- **Chrome-drop touch points** (feature-skills repo): `feature-context/SKILL.md:180`, `feature-requirements/SKILL.md:323` & `:430`, `feature-plan/SKILL.md:212`, `feature-review/SKILL.md:236`.
- **Clipboard-handoff touch points to rewire** (feature-skills repo): `feature-requirements` Steps 6/6b, `feature-review` Step 8, `feature-iterate` Step 1, `feature-plan` Step 4 (comments only — `feature-plan` has no synthesis doc; it triages inline).
- **No sibling-nav impact.** Comments add no doc type, so `DOC_TYPE_ORDER` (`inbox.py`) is untouched.

## Design notes

- **Comment submit reads the iframe's JS state, not the DOM** (round 1). The "mirror synthesis with no template change" claim only holds if the shell reads `iframe.contentWindow.comments` directly — the rail DOM holds only truncated, escaped projections. Same-origin, non-sandboxed access makes this work without a `postMessage` bridge or template change.
- **Replace-active-set on submit** (round 1). A submit replaces the doc's active comment set with the rail's current contents (integrated comments untouched), reconciling the "comments accrete" vs. "definitive submit" tension: the submit click is the terminal signal, and deletion in the rail propagates by replacement — no delete endpoint.
- **Mark integrated by id, not per-doc** (round 1). The agent marks only the comments it just read, so a comment left during an integration round isn't silently retired unread; this also underpins the human/agent concurrency story.
- **404-vs-indexed applies to both reads** (round 1). A freshly written feedback or spine doc 404s until the walker indexes it, for synthesis as much as comments; the skill force-walks then polls, treating 404 (not indexed) and 200-unsubmitted (awaiting human) as distinct.
- **Polling interval = 5 s, no hard ceiling** (round 1, Nigel — flagged "we should define this"). Fixed 5 s poll; review can take arbitrarily long, so no default timeout, just a periodic "still waiting" status and a manual give-up that falls back to the clipboard.
- **Clipboard fallback kept for unreachable + give-up** (round 1). Not removed in v1; the likelier trigger is a developer giving up the wait, not a down server. Full removal deferred.
- **SSE done-signal deferred** (round 1). Exposing `last_submit_at` / a count over SSE is unnecessary while submit is a clean terminal edge; revisit only if that proves insufficient.
- **Deep-link kept out of the headline flow** (round 1). The skill points at the inbox; optionally the read endpoint can return the doc's webapp URL cheaply, but per-doc deep-linking isn't a scoped deliverable.
