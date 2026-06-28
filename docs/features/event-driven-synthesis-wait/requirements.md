# event-driven-synthesis-wait — Requirements

## Summary

Today the `feature-requirements` skill posts a feedback doc to the inbox and then **busy-polls** the webapp every five seconds, waiting for the human to submit. Because a single agent turn can't block forever, the agent runs this poll as a background loop with a finite cap and is re-invoked each time the cap is hit — re-announcing “still waiting” each time.

When the reviewer steps away, this becomes noise. In the `server-rendered-docs` requirements round the human was absent for roughly eight hours: the loop ran a `curl` against the local server every five seconds the whole time, and the agent re-announced “still polling” around eight times as its background-turn cap expired and respawned. None of that told the developer anything they cared about — it cuts against the workflow's direction of travel, which is to surface only the decisions the developer wants and no more.

This feature replaces the busy-poll with an **event-driven wait**. The webapp already knows the instant a submission lands — the same in-process signal that live-refreshes the browser. We add a wait endpoint the agent calls once: it returns immediately if the answer is already in, otherwise it holds quietly and returns the moment the human submits, or after a bounded server-side timeout on which the agent silently reconnects without re-announcing. The five-second loop and the repeated “still waiting” lines both disappear.

## Scope

In scope:

- A new **wait** variant of the existing synthesis read endpoint in this webapp — a bounded, event-driven long-poll.
- Updating the shared polling convention (`docs/webapp-polling.md`) and the one skill that actually polls synthesis — `feature-requirements` — in the feature-skills repo to call it, with a silent reconnect on a clean timeout. (`feature-plan` and `feature-review` handle reviewer feedback as inline chat triage and never poll, so they are not touched — see Alternatives.)

The work spans two repos: the endpoint lives in feature-skills-webapp; the consumption change is a small edit in feature-skills.

## Vision

The agent posts a feedback doc, makes one wait call, and stays silent until the human submits — then continues instantly — with no five-second poll loop and no repeated “still waiting” messages, however long the human takes.

## Non goals

- **No multi-worker webapp.** The wait rides the existing in-process broadcaster. Many agents waiting at once — the multiple-simultaneous-features case — is fully supported, because the webapp runs as a single process (`uvicorn.run` with no `workers`) and every waiter shares the one broadcaster. The limitation is only running the webapp itself as multiple worker processes, which it is not deployed as; that would need the broadcaster reworked first and is out of scope.
- **No separate comment-wait endpoint.** Click-to-comment annotations are read opportunistically right after the synthesis submission arrives; they are not independently waited on. Waiting stays scoped to synthesis submissions.
- **No clipboard fallback.** The old “Copy responses” clipboard path is dead: synthesis docs now render in *synthesis-native* mode (the webapp parses the feedback items and re-renders them with its own template, which has only a server-POST “Submit response” button — no copy button), so there is no clipboard button to fall back to. The real degradation path is the existing short poll (see Technical approach).
- **No change to how submissions are written or stored.** No new tables and no schema change — the wait only reads existing synthesis state.

## User stories

1. As the developer, I want the agent to stay quiet while I'm away from a posted feedback doc, so that when I leave a requirements review for eight hours I come back to nothing but the agent ready to continue, not eight near-identical “still polling” lines.
2. As the developer, I want the agent to react the instant I submit, so that if I answer the synthesis doc thirty seconds after it's posted, the agent picks it up immediately rather than on the next five-second tick.
3. As the workflow author, I want the wait to degrade to the existing short poll when the long-held connection fails, so that a future reverse proxy that kills long-lived requests never blocks the workflow — the agent just drops back to the robust five-second GET it uses today.

## Technical approach

### A bounded “wait” sibling of the synthesis read endpoint

Add `GET /api/documents/{project}/{feature}/{doc_type}/{instance}/synthesis/wait` alongside the existing `.../synthesis` read. It returns the same JSON shape (`doc`, `submitted`, `responses`, `routine_flags`), so it is a drop-in replacement for the current poll; the only difference is *when* it returns.

### Register-then-check, so a submission is never missed

The handler registers on the in-process broadcaster (the same queue `/events` uses) *before* it reads the doc's current synthesis state. With the queue already registered, any submission that commits and broadcasts from that point on is buffered in the queue rather than lost. If the read then shows the doc is already submitted, it returns immediately; otherwise it blocks on the queue. This register-first ordering closes the race where a submission lands in the narrow window around the initial state check.

### Coarse signal, re-check on every wake

The broadcaster's `changed` message is contentless and fires on any committed change, not just this doc. So on each wake the handler re-reads this specific doc's synthesis state and returns only when *this* doc is submitted; an unrelated change simply sends it back to waiting. Multiple agents waiting concurrently is fine at this scale — each registers its own queue and re-reads independently.

### Bounded timeout, with cleanup guaranteed by the timeout

The wait holds for a finite server-side timeout and then returns `submitted=false`, so the agent is never blocked forever and the existing re-call path still works. The timeout is a named constant chosen to sit comfortably under the idle timeouts of the reverse proxy the eventual hosted deployment will sit behind — a concrete, self-contained bound — which lands it in the tens of seconds, reconnected each turn rather than held for minutes. (It should also be shorter than a typical agent background turn so the reconnect is clean, but that is a secondary comfort, not the sizing target: the turn budget is an agent-harness property, not something this webapp can observe.) Cleanup of the registered queue is guaranteed by this bounded timeout via a `try/finally` unregister, *not* by client disconnect detection — whether a plain JSON long-poll handler is cancelled on client disconnect is unverified for this stack, so a dead client's queue is reclaimed at timeout, at worst one window late.

### Skill side: one call, silent reconnect, poll fallback

The consuming skill replaces the five-second poll loop with a single wait call that it re-issues silently on a clean `submitted=false` timeout — no “still waiting” re-announcement. If the wait call errors or the long-held connection is refused, it degrades to the existing short five-second poll against the plain `.../synthesis` endpoint, which is robust to the connection/proxy failures that could break a long-held request.

## Testing

- An already-submitted doc returns immediately with the full responses payload and no blocking.
- A submission landing *during* the wait wakes it and returns the responses.
- A broadcast for a *different* doc does not satisfy the wait — it re-checks and keeps waiting (guards the coarse-signal case).
- The wait returns `submitted=false` when the timeout elapses with no submission.
- **The registered broadcaster queue is unregistered on every exit path** — submit, timeout, client disconnect, and error — so a wait never leaks a queue that future broadcasts keep filling. This is an acceptance criterion, not just an implementation note.
- A missing document returns 404; an unconfigured DB returns 503; a missing broadcaster degrades rather than crashing.
- A transient DB error on a wake-time re-read surfaces as an error response (so the skill falls back to polling) rather than silently looping.

The timing-sensitive cases (wake-on-submit, timeout) need deterministic control over the timeout rather than real sleeps, so the tests stay fast and stable.

## Alternatives

1. Block on the next event only, with no initial state check Weighed against the “don't miss a submission” constraint (context doc) Races: if the human submits before the agent starts waiting, the event is already gone and the wait hangs until timeout. Rejected in favour of register-then-check.
2. Switch all three feedback skills to the wait endpoint The draft's original Phase 2 scope; corrected against the skill files (review round 1) Only `feature-requirements` polls synthesis (a 5 s loop, `SKILL.md:399–412`). `feature-plan` (`SKILL.md:317`) and `feature-review` (`SKILL.md:274`) already eliminated the re-announce noise by handling feedback as inline chat triage — living proof the prompt-only fix suffices for short rounds. The event-driven wait earns its keep specifically for `feature-requirements`' long, possibly-overnight synthesis waits, so Phase 2 touches only it plus the shared convention.
3. A generic doc-wait reporting which of submission / comments changed Open question in the context doc More surface than the pain needs. Comments are read once the synthesis wakes the agent, not waited on independently. Kept the endpoint scoped to synthesis; a generic wait can follow if a real need appears.
4. Have the wait endpoint subscribe to `/events` over HTTP internally Open question in the context doc Unnecessary indirection — the broadcaster is in-process, so the handler registers on it directly, exactly as `/events` does. An HTTP self-call adds a hop for no benefit.
5. A `?wait=30` query param on the existing endpoint instead of a `/wait` sibling Reviewer-suggested alternative (review round 1) Would share the handler outright and avoid a second route. Both are fine; chose the explicit `/synthesis/wait` sibling for a cleaner, separately-documented contract. Noted so the choice is on record.
6. Just stop the skill re-announcing, and keep the 5 s poll Open question in the context doc (“right-sized fix?”) Removes the message noise but leaves the continuous five-second curl loop and the up-to-5 s wake latency. The endpoint and the skill instruction change are complementary — we do both, split across the two phases.

## Delivery phases

### Phase 1 — Wait endpoint in the webapp

Add the bounded, register-then-check `.../synthesis/wait` endpoint that returns the synthesis payload on submit or `submitted=false` on timeout, riding the existing broadcaster, with the guaranteed queue cleanup above. Ships with the tests in the Testing section. Delivers a usable, independently testable capability with no skill changes yet. One MR in feature-skills-webapp; must merge before Phase 2.

### Phase 2 — Skill adoption in feature-skills

Update `docs/webapp-polling.md` — including removing its “emit a ‘still waiting…’ line every 60 s” instruction — to document the wait protocol (single held call, silent reconnect on a clean timeout, short-poll degradation, no clipboard path). Switch `feature-requirements` to use it: replace the 5 s poll and the 60 s “still waiting” line (`SKILL.md:399–412`) with the wait call. One MR in the feature-skills repo.

## Indicative notes

- Endpoint `GET /api/documents/{project}/{feature}/{doc_type}/{instance}/synthesis/wait`, registered next to the existing `get_document_synthesis` route in `web/app.py`; handler likely in `web/submit.py` beside it.
- Reuse `get_document_synthesis`'s read (logical-key lookup plus the `synthesis_responses` query) for both the initial check and the per-wake re-check, to keep one source of truth for the response shape.
- **Order matters:** `register()` the queue first, then do the authoritative read, then block on `asyncio.wait_for(q.get(), timeout=…)`. Registering before the read is what makes the wait race-free; wrap everything in a `try/finally: unregister(q)`, mirroring `web/events.py`.
- Optionally add a best-effort `Request.is_disconnected()` check on each wake to free a dead client's queue before the timeout — a nicety, since the timeout is the actual safety net.
- Timeout as a module-level named constant (consider an env override) so the hosted-proxy value can be tuned without a code change.
- The current synthesis poll is already **logical-key** addressed, not path-keyed — the context doc's “path-keyed” note predates that. The wait mirrors the logical-key form, so the skill change is minimal.

## Design notes

- **Phase 2 scope narrowed to `feature-requirements` + the convention doc** (review round 1). Verified that `feature-plan` and `feature-review` do not poll synthesis — they triage feedback inline in chat — so they need no change.
- **Clipboard fallback removed** (review round 1, developer comment). Confirmed in code: synthesis docs render in *synthesis-native* mode (`web/doc_view.py`), re-rendered from a server template (`templates/doc.html`) whose only action is a server-POST “Submit response” button; no “Copy responses” button is served. The degradation path is the existing short poll.
- **Timeout justified by the proxy/connection bound**, not the agent background-turn budget (review round 1). The turn budget is an agent-harness property this webapp can't observe; sizing a server constant to it would couple the webapp to runtime internals.
- **Register-then-check ordering** chosen as the race-free sequence (review round 1).
- **Cleanup guaranteed by the bounded timeout**, not by disconnect detection (unverified for plain JSON long-poll handlers) (review round 1).
- **Concurrent waiters supported** because the webapp is single-process; multi-worker is the only unsupported case (developer comment).
