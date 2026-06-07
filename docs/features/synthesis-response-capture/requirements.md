# synthesis-response-capture

## Problem

When `feature-skills` runs a review round, it generates a synthesis doc — a feedback HTML like `requirements-feedback-1.html` with a textarea per triaged item — and opens it in Chrome. The developer fills in their responses, clicks **Copy responses**, and the page builds a JSON blob on the clipboard. They then switch to the terminal and paste it into the Claude Code conversation so the agent can integrate the feedback.

That clipboard round-trip is the weak link:

- **There's no server-side record of the response.** A filled-in answer has exactly one exit — onto the clipboard and into the chat. Nothing else can see it: not the inbox, not the agent, not a second glance later.
- **The clipboard is a single, fragile slot.** Copy a Slack link or anything else between "Copy responses" and the paste and the response is silently lost — and there's no saved copy to fall back on.
- **The handoff is manual and out-of-band.** Copy, alt-tab, paste — every round. The webapp is already open and already renders the doc, but it's a dead end: it can't take the response.
- **The inbox can't help.** A synthesis doc waiting on the developer is invisible. `inbox-view` deliberately deferred its "Awaiting your input" category to this feature, because the state it keys on — captured responses — isn't stored anywhere yet.

There's also a structural gap behind all of this: **synthesis docs aren't indexed.** The walker types a doc from its `<meta name="feature-doc-type">` tag, and feedback docs carry none (confirmed against both `feedback-template.html` and real feedback docs in the store). So today they never enter the `documents` table at all — the webapp can render one if handed its id, but has no row to attach a response, an inbox card, or a read-stamp to.

## Vision

Answer a synthesis doc's questions in the webapp and click submit — the response is saved server-side, survives a closed tab, is available for the agent to pick up, and the inbox shows you exactly which docs are still waiting on you — with no clipboard and no paste-into-chat.

## User stories

1. As a developer reviewing a synthesis doc
  I want to submit my responses from the webapp and
        have them saved server-side
  I finish a requirements-feedback doc and click
        submit — my response is recorded server-side then and there. No alt-tab,
        no paste, and a stray clipboard copy afterwards can't lose it.
2. As a developer with several reviews in flight
  I want the inbox to show which synthesis docs are
        awaiting my input
  I open the inbox in the morning and an "Awaiting
        your input" group lists

  ; I'd forgotten that round was still open on me, and
        click straight through to finish it.
3. As a developer handing feedback back to the agent
  my submitted response to be available for the Claude
        Code session to retrieve
  After I submit in the webapp, my responses are
        exposed over a read endpoint keyed by the doc's path — the same identity

  already knows it by — so the session can pull
        them instead of waiting for a pasted JSON blob. (The skill-side change
        that actually consumes this endpoint lands in

  ; this feature delivers the
        webapp surface it reads from.)

## Data model

The store already anticipates this feature. The `synthesis_responses` table exists from the very first migration but has never had a writer: it holds one row per answered item, keyed by the document and the item number, carrying the free-text response, an optional routine-flag note, and an updated timestamp. No migration is needed for the responses themselves.

What's missing is the thing those rows point at. A synthesis doc has to be a real row in `documents` before a response can hang off it, and today it isn't one. So this feature must first get synthesis docs **indexed** — discovered, typed, and tracked through the same active / archived lifecycle as every other doc.

"Awaiting your input" is a **doc-level** state, not a per-item one: a doc is awaiting while it is active and *no submission has been recorded for it yet*. This deliberately avoids a per-item completeness predicate, which the doc's own semantics make incoherent — an empty textarea means "agree with your take" (a complete answer), and a routine item is normally left un-flagged. Keying on "has any submission happened?" sidesteps both. A consequence: the webapp does *not* need to parse the doc's item set to drive the inbox; recording the item set as lightweight document metadata is optional, worth it only if we later want a "3 of 8 answered" progress indicator.

Relationships: one synthesis *document* has many *items*; each item has zero or one captured response. A submission **replaces** the document's whole set of responses (so stale rows can't linger if a regenerated doc changes its items). Responses are keyed to the document and survive its lifecycle transitions — they persist when the walker marks a doc archived or missing, and are removed only if the document row is hard-deleted. Once a round is integrated, `feature-skills` moves the doc into `.feedback-archive/`, the walker marks it archived, and it drops out of the awaiting set while its captured responses remain available to read.

For retrieval, a response is addressed by the document's *source path* — the identity `feature-skills` already holds for a doc it generated — rather than the webapp's internal numeric id, so the agent can read back what it can't otherwise map.

## Technical approach

### Index synthesis docs through a meta-tag-less typing path

The walker types every doc from its `feature-doc-type` meta tag, and bails (counting an error) when there's none — which is exactly why feedback docs are invisible today. So this isn't a small tweak to the identity step: the walker gains a *dedicated typing path* that recognises synthesis docs by their filename shape (`*-feedback-N.html`) and assigns them a synthetic type without needing a meta tag. This is chosen over adding a meta tag to `feature-skills`' feedback template because that's a cross-repo change that wouldn't retroactively fix docs generated by older skill versions or already sitting in the store. It is not coupling-free — it trades an *explicit, declared* coupling (a tag) for an *implicit, conventional* one (the filename pattern), so a rename on the feature-skills side would silently break indexing. That's acceptable precisely because the two repos are already version-coupled. Active synthesis docs (mid-round) sit beside their feature; archived ones live under `.feedback-archive/`, the active/archived split the walker already models.

### Capture and read responses over HTTP

A write endpoint accepts the *same payload shape the "Copy responses" button already produces* (so the existing client logic is reused, not reinvented) and **replaces** the document's full set of responses in one transaction — so re-submitting is idempotent and stale items from a regenerated doc can't linger. It follows the existing admin-route conventions: `503` when the DB is unconfigured, `404` for an unknown doc, `400` for a malformed payload, `200` with a small acknowledgement on success. The doc the response attaches to is the one the `doc-view` shell is showing — the parent frame's document id is authoritative; the path-style `doc` field in the body is at most validated against it, never trusted for routing. A companion read endpoint, keyed by the document's *source path*, returns a doc's stored responses in the same shape (with a clear "not submitted yet" state), giving the Claude Code session a way to pull the feedback by an identity it already holds.

The endpoints write developer free-text into the DB from a browser POST, so the trust boundary is worth stating: the server binds `127.0.0.1` only, and that local-only binding *is* the boundary — no per-user auth is warranted for a single-user local tool. Response text is length-capped (at 1 MB) defensively rather than restrictively. Concurrency needs no new machinery: a double submit is last-write-wins, and the agent reading while the walker re-indexes is already covered by the per-request-connection + WAL discipline, so reads see committed state.

### An "Awaiting your input" inbox category

The inbox read-model gains a fourth category alongside new / in-progress / shipped: active synthesis docs *with no submission recorded yet* (the doc-level predicate above). This is the category `inbox-view` stubbed out and pointed here. A doc that is awaiting your input is *excluded* from "New since last visit", so it reads as one unambiguous call-to-action in its own group rather than appearing twice — the two categories answer different questions ("something changed" vs "you owe a response") and the stronger signal wins. Submitting emits a `changed` event over the existing `sse-refresh` broadcaster, so an open inbox tab live-drops the doc out of the awaiting group without a manual reload.

### Submit from the webapp without coupling the template to the webapp

Synthesis docs render inside `doc-view`'s pass-through iframe, so the doc's own JS runs intact. The submit affordance lives on the `doc-view` shell (the parent frame), which already knows the document id and the endpoint. On submit it asks the embedded doc for the responses it has assembled — via a small `postMessage` bridge, validating message origin and payload shape — and POSTs them. This keeps `feature-skills`' template unaware of the webapp's URLs or document ids; its only addition is replying to a "hand me your payload" message, a backwards-compatible change. The iframe is rendered same-origin (no sandbox), so the origin check is a cheap honesty about the trust model rather than a hard isolation boundary. (The webapp and `feature-skills` are already version-coupled — the README pins `feature-skills ≥ v2.1` — so a coordinated minor bump is an established pattern, not a new burden.)

## Alternatives considered

1. Add a

  meta tag to the feedback template
  Source: derived from how the walker types every other doc
  Would let the walker index feedback docs the same
        way it indexes the rest — but it's a cross-repo change, and synthesis docs
        generated by an older

  version (or already
        sitting in the store) would still lack the tag and stay invisible.
        Filename-pattern recognition is retroactive and self-contained in the
        webapp. It is not coupling-free — see the Technical approach: it swaps an
        explicit declared coupling for an implicit conventional one — but that
        trade is acceptable given the existing version coupling.
2. Have the webapp re-render its own response form from parsed items
  Source: obvious alternative given doc-view already parses docs
  Rejected: it duplicates the synthesis doc's
        rendering and would drift from

  ' template over
        time.

  deliberately chose iframe pass-through so the
        doc's own markup and JS stay authoritative; reusing that is simpler and
        truer to the source.
3. POST directly from the synthesis doc's own JS
  Source: simplest conceptual path
  Deferred: it forces the statically-generated
        template to know the webapp's base URL and to have a document id injected
        into it. The

  bridge keeps the template URL- and
        id-agnostic, at the cost of one small message handler.

## Delivery phases

### Phase 1 — Index synthesis docs

Give the walker a dedicated, meta-tag-less typing path that discovers, types, and lifecycle-tracks synthesis docs by filename. No user-visible change yet, but it unblocks everything downstream: synthesis docs become addressable `documents` rows. Parsing the item set into metadata is optional here — the doc-level awaiting predicate doesn't need it — so it's deferred unless a progress indicator wants it later. Verified by walker tests over a docs tree containing feedback docs, active and archived.

### Phase 2 — Capture & retrieve responses

The write endpoint (replace-on-submit into `synthesis_responses`, parent-frame document id authoritative, 1 MB cap) and the read endpoint (return a doc's responses keyed by source path, in the established payload shape, with a "not submitted yet" state). Testable end-to-end over HTTP independent of any UI: POST a payload, GET it back, confirm replace semantics and the 400 / 404 / 503 contract.

### Phase 3 — "Awaiting your input" in the inbox

The fourth inbox category (active docs with no submission yet) and its card rendering, excluding awaiting docs from "New since last visit" so each reads as a single call-to-action, plus a `changed` broadcast on submit so an open tab live-updates. Delivers the at-a-glance "what's waiting on me" the inbox stubbed out.

### Phase 4 — Submit from the webapp

The `doc-view` submit affordance and the `postMessage` bridge to the rendered synthesis doc, plus the one backwards-compatible addition to `feature-skills`' feedback template (replying to the "hand me your payload" message) — the point at which a developer can capture a response in the webapp instead of the clipboard. The complementary skill-side change — the feature-skills review skills *reading* responses back from the read endpoint, and dropping Chrome/clipboard entirely — is out of scope here and lands in `skill-integration-parallel`. Until then the clipboard remains as a fallback path. Phases 1–3 stand on their own regardless.

## Indicative implementation notes

Plan-level detail surfaced during requirements exploration, carried forward for `/feature-plan`. Not binding.

- **Existing table.** `synthesis_responses` is defined in `0001_init.sql`: `PRIMARY KEY (document_id, item_num)`, nullable `response` and `routine_flag`, `updated_at NOT NULL`. No migration required. `ON DELETE CASCADE` on `document_id` means responses vanish only on hard delete; the walker archives/reactivates via `UPDATE` (same row id), so they survive those transitions.
- **Payload shape** (from the feedback template's "Copy responses" button — the write endpoint accepts this verbatim): Empty string in `responses` = "agree"; `routine_flags` entries are routine items flagged for discussion. A submit does a delete-then-insert of the doc's whole row set (replace-on-submit), so the body's `doc` field is ignored for routing — the parent frame's document id wins.
  ```json
  { "doc": "docs/features/<FEATURE>/<phase>-feedback-<N>",
    "responses":     { "1": "free text", "2": "" },
    "routine_flags": { "19": "comment" } }
  ```
- **Read key.** The read endpoint is keyed by `documents.source_path` (the dev-store absolute path) rather than the numeric id, since that's the identity feature-skills holds. Will need a normalisation between the agent's path-style id and the stored absolute path. "Not submitted yet" → 404 or empty body (pick one in the plan).
- **Awaiting predicate.** Doc-level: active synthesis doc with no row in `synthesis_responses` yet (i.e. never submitted). No item-set parsing required for this; item-set metadata in `documents.metadata_json` stays optional/deferred.
- **Walker typing.** `parse_doc_html()` reads `<meta name="feature-doc-type">` and `_process_file` counts an error + skips when it's absent — so the synthetic typing path is a real fork, not an `identity_for` tweak. Recognise by filename (`*-feedback-N.html`) at the existing depth-3 (active) and depth-4 `.feedback-archive/` (archived) identities. Pick a stable `type` string and a `humanise_type()` label; note `DOC_TYPE_ORDER` (`inbox.py`) has no feedback entry, affecting doc-view sibling-nav ordering.
- **Inbox interaction.** `new_since_last_visit` INNER-JOINs features and filters `status='active'`; awaiting docs must be excluded from it (decided) so they don't double-list.
- **Route & conventions.** Mirror `admin_mark_read` / `admin_discover` in `web/routes.py`: per-request connection via `request_conn`, writes inside `transaction()`, timestamps via `now_iso()`; 503 unconfigured DB, 404 unknown doc, 400 malformed payload, 200 + ack on success. SSE submit-broadcast reuses `web/broadcaster.py` from `sse-refresh`.
- **Read-state.** No extra stamping on submit — doc-view already stamps read on shell render, so the doc is read before submit.

## Design notes

- **Awaiting is doc-level, not per-item** (round 1). A doc is "awaiting your input" until any submission is recorded for it. A per-item predicate is incoherent here: an empty textarea is a valid "agree", and routine items are normally un-flagged. This also makes item-set parsing optional.
- **Submit-only persistence** (round 1). Responses are saved on submit, not autosaved per keystroke — the Problem section was softened to not promise crash-safety it won't deliver. Autosave was considered and dropped, with no follow-up tracked (per Nigel).
- **Read by source path** (round 1). The read endpoint is keyed by `documents.source_path`, the identity feature-skills already holds, resolving the path→id gap rather than deferring it.
- **Replace-on-submit** (round 1). A submission replaces the doc's full response set in one transaction, so a regenerated doc with a changed item set can't leave stale rows.
- **Scope boundary with skill-integration-parallel** (round 1). This feature delivers the webapp surface (index, capture/read endpoints, inbox, submit UI). The feature-skills skill change that *consumes* the read endpoint and retires Chrome/clipboard lives in `skill-integration-parallel`; the clipboard stays as a fallback until then.
- **Response cap 1 MB** (round 1, Nigel) — defensive, not restrictive.
- **Feedback docs excluded from sibling-nav** (plan review). Once indexed, active feedback docs are kept out of doc-view's prev/next sibling navigation — they're transient review artefacts, not part of the context→requirements→plan→review spine. Starting them out; cheap to revisit.
- **Submit reads the iframe DOM directly** (plan review). The doc-view shell reads the same-origin, non-sandboxed feedback iframe's fields and POSTs them, rather than the postMessage bridge the requirements first proposed — no feature-skills template change, works on existing docs.
- **No "consumed" marker** (round 1). Unlike `comments` (`status`/`integrated_at`), `synthesis_responses` gets no integration-state column in v1; the agent reads on demand within a round, so staleness isn't yet a problem. Noted as a known limitation.

## Review decisions

### Round 1 (post-merge review)

- **Fixed:** the write endpoint's 1 MB cap was behind an `isinstance(val, str)` guard, so a non-string response value slipped past it and was stored as-is. Now non-string `responses`/`routine_flags` values are rejected with a 400, with a test.
- **Declined (moot):** parenthesising the exception tuple. `ruff format` with `target-version = "py314"` and the `UP` lint rules *enforces* the bare `except ValueError, TypeError:` form — it strips the parens on format, so the suggestion can't be applied. The 3.14 floor is intentional (`requires-python >= 3.14`).
- **Declined:** validating against negative `item_num` keys. The real client never emits them and nothing downstream breaks on a negative item number, so the guard would cover a case that can't occur.
