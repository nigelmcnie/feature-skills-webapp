# event-driven-synthesis-wait

## Problem space and motivation

The requirements, plan, and review skills hand a synthesis (feedback) doc to the human in the inbox, then **busy-poll** `GET /synthesis-response?path=…` every 5 seconds waiting for the submission, emitting a "still waiting" line roughly every 60 seconds. Because a single agent turn can't block indefinitely, the agent runs this as a background loop with a finite cap and is re-invoked each time the cap is hit.

The cost shows up when the reviewer steps away. In the `server-rendered-docs` requirements round, the human was absent for roughly eight hours; the background poll timed out about hourly, and each time the agent re-spawned the loop and re-announced that it was "still polling" — around eight near-identical status messages, plus a continuous 5-second `curl` loop against the local server the whole time. That is noise: it rubs against the workflow's direction of travel — *surface only the decisions the developer cares about, and no more* — and it gets worse as the process moves toward hosted, multi-machine, possibly-overnight waits where the gap between "doc posted" and "human answers" is routinely long.

The webapp already knows the moment a submission lands; the agent shouldn't have to keep asking. An event-driven wait would let the agent make a single call, idle silently, and wake exactly when the human submits (or when a long server-side timeout elapses) — removing both the 5-second loop and the repeated re-announcements.

## Related work

**The broadcaster + SSE infrastructure already exists.** The `sse-refresh` feature built an in-process `web/broadcaster.py` (one `asyncio.Queue` per connected client) and a `/events` `EventSourceResponse` endpoint that emits a contentless `changed` message on connect and on every committed change. This is the natural backbone for a wait endpoint — the agent's wait can hang on the same signal the browser already uses to live-refresh.

**The submission endpoints already broadcast.** Both `POST /doc/{id}/synthesis-response` (`web/synthesis.py`) and `POST /doc/{id}/comments` (`web/comments.py`) call `request.app.state.broadcaster.broadcast()` on commit, so a submission already produces an in-process event a waiter could observe.

**The current polling convention is documented once.** The feature-skills repo's `docs/webapp-polling.md` defines the shared "force-walk → 5 s poll → clipboard fallback" convention that `feature-requirements` (Step 6), `feature-plan`, and the review skills all follow. This feature would update that single convention plus the skills that reference it.

Related read endpoints that a waiter complements rather than replaces: `GET /synthesis-response?path=…` and `GET /comments?path=…` (both path-keyed, returning a `submitted` flag).

## Constraints and considerations

- **Bounded wait, always.** The endpoint must hold for a long but finite server-side timeout and then return a "not yet" result, so the agent is never blocked forever and the existing re-call/fallback path still works.
- **Keep the clipboard fallback.** If the server is unreachable (or the wait errors), the skills must still degrade to the "Copy responses" clipboard paste — the wait is an optimisation over polling, not a hard dependency.
- **Don't miss a submission that already happened.** A pure block-on-next-event design races: if the human submits before the agent starts waiting, the event is gone. The wait should check current state first and only then block on the next event (check-then-wait), so an already-submitted doc returns immediately.
- **Broadcaster signal is coarse.** The `changed` event is contentless and fires on any committed change, not just the doc being waited on. The waiter will likely re-check the specific doc's state on each wake rather than trusting the event to mean "your doc". Acceptable, but worth designing deliberately.
- **Path- vs id-keyed.** The current GETs are path-keyed (the dev-store path the agent already holds). A wait endpoint should match that addressing so the skills change minimally — though note the broader arc (`agent-submission-api`) is moving toward logical-key addressing.
- **Connection longevity.** A long-held HTTP request needs to survive whatever sits in front of the server. There is no reverse proxy today, and the default hold is 240 s (just under the 5-minute Anthropic prompt-cache TTL, to keep a reconnecting client cache-warm); a *future* reverse proxy must set `proxy_read_timeout` ≥ the hold. The skill side re-issues the wait on a clean timeout return and degrades to a 5 s short poll if the hold is refused, so it fails safe.
- **Scope spans two repos.** The endpoint is a feature-skills-webapp capability; consuming it is a small change to `docs/webapp-polling.md` and the three skills in the feature-skills repo.

## Links

- Surfaced by: the `server-rendered-docs` `/feature-retro` (this session) — the ~8-hour hourly re-announce.
- Backbone: `sse-refresh` — `web/broadcaster.py` + `/events`.
- Convention to update: `docs/webapp-polling.md` (feature-skills repo).
- Endpoints: `web/synthesis.py`, `web/comments.py`.

## Open questions

1. One wait endpoint or two? A `/synthesis-response/wait` plus a `/comments/wait` sibling, or a single generic doc-wait that reports which of submission/comments changed?
2. What server-side timeout balances "hold long enough to be useful" against proxy/ connection limits in the eventual hosted deployment — and what does the skill do on a clean timeout (re-issue silently vs. one brief status line)?
3. Does the waiter block on the broadcaster directly, or subscribe to `/events` internally? (The broadcaster is in-process, so a direct subscription is simplest, but a multi-process/hosted future might change that.)
4. Should the wait also cover the requirements/plan *comment* round-trip, or just synthesis submissions, in the first cut?
5. How much of the "surface only what I care about" win is the endpoint vs. simply instructing the skills to stop re-announcing on each poll cycle? (Worth confirming the endpoint is the right-sized fix, not just a prompt tweak.)
