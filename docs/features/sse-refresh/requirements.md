# sse-refresh

## Problem

The webapp's whole pitch (design doc §1) is "a single always-open tab that pushes new docs" — replacing the old flow where every feature-skills step popped a fresh Chrome window. The inbox at `/` already delivers the cross-project view, and `doc-discovery` indexes new and changed docs into SQLite within sub-seconds via a `watchfiles` filesystem watch.

But the open browser tab never finds out. The inbox page is a static, server-rendered snapshot taken at page-load time. While an agent writes a `requirements.html` in another terminal, the tab the user is staring at shows nothing new until they manually reload. The one piece of reload-on-its-own behaviour that exists — the `pageshow` bfcache handler — only fires on back-navigation, not while a tab sits open.

So the user has to remember to refresh to notice that requirements are ready for review, that a doc changed, or that a feature shipped. That defeats the "always-open, it just updates" promise and pushes the webapp back toward "static file server with extra steps."

## Vision

The open inbox tab updates itself within a second of a doc landing, changing, or shipping — no manual reload, ever.

## User stories

1. As a developer with the webapp open while an agent works
  I want a newly-written doc to appear in the inbox on its own
  I kick off

  in a terminal and switch to the browser. Seconds later a new "Requirements" card appears under "New since last visit" without me touching anything.
2. As a developer reviewing long-running agent work
  I want ships and updates to reflect in the inbox as they happen
  A session I left running finishes and the tracker flips a feature to done. The card moves itself into "Recently shipped" while I watch, instead of after I next reload.
3. As a developer who leaves the tab open for hours
  I want a long-idle tab to re-sync itself, not show a frozen snapshot
  I come back to a tab I opened this morning — the machine slept in between, so the stream had dropped. On reconnect the tab refetches current state and shows it, rather than sitting on this morning's snapshot until I reload.

## Data model

No schema changes. The existing `events` table remains the source of truth for "something changed" — every discovery (created / updated / archived / missing / reactivated) and every `shipped` transition already writes an events row inside the walk.

The realtime layer adds no persisted state. The set of connected clients and the change signal that fans out to them live entirely in memory for the lifetime of the process; nothing is stored. There is deliberately no replay of missed events across reconnects, and none is needed: because the signal is contentless and the client always responds by refetching current state, a missed ping costs nothing — the next refetch is a full snapshot that subsumes anything missed.

## Technical approach

### In-process broadcast off the walk worker

A small in-memory broadcaster lives on `app.state` and holds the connected clients. The single serialised walk worker — the only writer of `events` rows — signals the broadcaster after any walk whose summary shows a net change. Because the worker is already the in-process serialisation point for all change detection, it can push directly; there is no database polling and no separate change-watcher to build.

**Ships must broadcast, by design and not by coincidence.** A `shipped` event (a feature's done-transition in the tracker) must trigger a refresh. Today that happens incidentally because the `features.html` edit also registers the tracker doc as `updated`, but the requirement is that ships broadcast on their own terms — the change signal must account for shipped events explicitly, so a future change that emits a ship without touching a tracked document row can't silently stop refreshing the inbox.

### Correctness constraints on the signal

- **Signal from the event loop, never from the worker thread.** The walk itself runs in a worker thread (`asyncio.to_thread`), and the broadcaster is an asyncio structure that is not thread-safe. The broadcaster must therefore be signalled from the worker *coroutine* after the threaded walk returns — back on the event loop — not from inside the threaded walk. This is a correctness requirement, not an implementation nicety.
- **The change is committed before it is broadcast.** The threaded walk commits and closes its database transaction before returning, and the broadcast fires only after that return. So any client that refetches in response to a signal is guaranteed to see the committed rows — there is no broadcast-before-commit window.
- **Single process.** The in-memory broadcaster is correct only under a single server process (which is how the app runs today, both standalone and under systemd). A multi-worker deployment would leave a client connected to one worker deaf to changes detected by another; the broadcaster assumes one process and that assumption must be stated, not discovered.

### A coarse "changed" signal, not granular deltas

The server pushes a contentless "inbox changed" ping. The client reacts by re-fetching a rendered inbox fragment and swapping the card region in place. We deliberately do not compute per-document deltas or patch individual cards: for a single-user, single-tab, localhost inbox the whole-region swap is simpler, robust to any category movement (a card moving from "New" to "Recently shipped" needs no client-side diffing), and the inbox query is already cheap.

### One client rule: refetch on any event, including on connect

The stream emits an event immediately when it opens — on first connect and on every automatic `EventSource` reconnect — in addition to the live `changed` pings. This collapses the client to a single rule: *on any event, debounced refetch*. It unifies the two cases that would otherwise need separate handling — a new doc landing (live ping) and a long-idle tab whose stream dropped while the machine slept (reconnect fires, tab self-heals on the connect event). A `visibilitychange` refetch is kept as cheap belt-and-braces for the case where a tab is refocused without the connection having dropped. The refetch — not `EventSource`'s auto-reconnect — is the load-bearing mechanism for re-syncing stale content; reconnection alone re-opens the stream but does not refresh what's on screen.

### SSE transport

The `/events` endpoint streams via `sse-starlette`'s `EventSourceResponse`, which handles event framing, periodic keepalive pings, client-disconnect cleanup, and the correct no-cache/no-buffering response headers for us rather than us hand-rolling that logic. On the client, a small amount of vanilla JavaScript opens an `EventSource` on `/events` and performs the debounced refetch-and-swap. No HTMX and no front-end framework — consistent with the current zero-dependency front end.

**The stream holds no database connection.** The rest of the app uses short-lived, per-request SQLite connections; a `/events` stream lives for hours. Since the signal carries no database data, the stream must hold zero DB handles for its lifetime — refetches go through the ordinary short-lived inbox request. This preserves the app's "no long-lived DB handle" invariant that its clean-shutdown behaviour relies on.

### The refetched fragment

The client refetches a rendered inbox fragment — the card region only — and swaps it in. Whatever endpoint serves that fragment, its behaviour is pinned: it must honour the same active `?project=` filter as the current view, and carry the same `Cache-Control: no-store` semantics the full inbox page already sets. A cached fragment would defeat the feature.

### Throttling

Signalling once per completed walk is already coarse: `watchfiles` debounces filesystem bursts and the walk worker batches queued requests, so a flurry of file writes collapses into few walks. A short client-side debounce coalesces any remaining back-to-back signals — across both the server pings and the connect/focus refetch triggers — so the inbox never repaints in a tight loop.

### Constraints & scope boundaries

- Local-only, single user, no auth — unchanged from the rest of the webapp. SSE binds to the same `127.0.0.1` server.
- Multi-tab fanout works for free (all tabs share the broadcaster) but is not a design goal we test against.
- **Mark-read is out of scope as a broadcast trigger.** It is a deliberate user action in the same tab that already round-trips, so that tab can reflect it directly; broadcasting it to other tabs is deferred.
- Graceful degradation: when the DB or discovery isn't configured, the `/events` endpoint must behave sanely (e.g. open, emit its connect event, then sit idle) rather than erroring, mirroring how `index` and `healthz` already guard on `db_path is None`.

## Alternatives considered

1. HTMX SSE extension for the swap
  Source: design doc §6 (sse-refresh card) — "HTMX swap on the inbox card list"; revisited with user
  Declarative and tidy, but pulls HTMX plus its SSE extension into a front end that has zero HTMX today. The vanilla

  + fragment-swap path is ~30 lines, adds no bundled assets, and matches the existing code. Chosen against HTMX.
2. Hand-rolled StreamingResponse
  Source: discussed with user
  Avoids a new dependency, but we'd then own keepalive, disconnect detection, and SSE framing plus their tests.

  is small and battle-tested; the dependency is worth it over bespoke streaming code.
3. Granular per-document payloads + client-side card patching
  Source: design doc §6 — "refreshes the affected card"
  More surgical repaints, but materially more client complexity (diffing, card lifecycle, category movement) for no real benefit at single-tab / localhost scale. The coarse re-fetch-and-swap is chosen; granular patching can be revisited if the inbox ever grows expensive to render.
4. Client polling instead of SSE
  Source: design doc §3 (Architecture chose SSE)
  A periodic

  would be simpler still, but the design explicitly chose push semantics ("always-open tab that pushes"). Polling trades latency and wasted cycles for marginal simplicity; rejected.
5. Database-level change notification
  Source: codebase observation
  Polling

  or hooking SQLite update callbacks to trigger the broadcast is unnecessary: the walk worker is the single in-process writer of events and can call the broadcaster directly. No DB-watching layer needed.
6. Separate reconnect-refresh and focus-refresh mechanisms
  Source: review round 1
  An earlier draft handled "new doc landed" and "stale tab refocused" with two distinct client paths. Emitting an event on connect collapses both into one rule (refetch on any event), so the separate paths were dropped in favour of the unified approach.

## Delivery phases

### Phase 1 — Server-side SSE

Add `sse-starlette`; introduce the in-process broadcaster on `app.state`; build the `/events` `EventSourceResponse` endpoint (emit a connect event on open, stream `changed` events, keepalives, clean up the client on disconnect, hold no DB connection); and have the walk worker signal the broadcaster — from the event loop, after the threaded walk returns — on any net change including ships. **Testable value:** `curl -N localhost:8800/events` emits an event on connect and again when a doc is written into the dev-store and discovered. Behaves sanely when DB/discovery are unconfigured.

### Phase 2 — Live inbox

Extract the inbox card region into a fragment with a single source of truth for card markup, served such that it honours the active project filter and `no-store`; add the vanilla `EventSource` client that refetches the fragment and swaps it in on any event, with a short debounce and a `visibilitychange` refetch as belt-and-braces. **Testable value:** open the tab, write a doc in another terminal, and watch the card appear under "New since last visit" without a manual reload; a tab whose stream dropped re-syncs on reconnect.

## Indicative implementation notes

Plan-level detail captured here so it isn't lost; the plan skill turns this into the implementation plan.

- **Broadcaster shape:** an asyncio fanout — a set of per-client `asyncio.Queue`s; the signal puts a sentinel on each. Created in the lifespan alongside the existing `walk_queue`, stored on `app.state`. Each `/events` connection registers its queue and removes it in a `finally` on disconnect. Signalled from the `_worker` coroutine after `await asyncio.to_thread(...)` returns (see the cross-thread correctness constraint above).
- **Ship-aware gate:** have `WalkSummary` carry a `shipped` count and broadcast when any of created / updated / archived / missing / reactivated / shipped is non-zero — so ships broadcast explicitly rather than riding on the tracker doc's incidental `updated` event.
- **Fragment endpoint:** extract the card region into a partial that is the single source of truth for card markup, reused by the full inbox page and the fragment response. Expose it via either a query flag on the index route or a dedicated route; the client must forward the current `project` param and the response must set `Cache-Control: no-store`. Leave the route mechanics to the plan.
- **Testing (dependency-free):** make the broadcaster fanout the first-class test — unit-test register / signal / fan-out / deregister directly, including a leak check that the client set returns to empty after N connect/disconnect cycles. This sidesteps the ASGI-transport/lifespan/socket concerns entirely. For any wired-endpoint test, note that `httpx.ASGITransport` does not run `lifespan` by default (where the broadcaster is created), so the broadcaster must be constructed explicitly or lifespan managed in-test, and every stream read must be bounded by `asyncio.wait_for` — a hung SSE read would stall an xdist worker (there is no `pytest-timeout` configured). Prefer this over adding `asgi-lifespan` / `pytest-timeout`. The suite runs under pytest-socket (`--disable-socket --allow-unix-socket`); the in-process ASGI transport needs no real sockets.
- **Tuning knobs:** keepalive ping interval (≈15s) and client debounce window (≈250ms) — pick conservative defaults. The debounce must coalesce across server pings and connect/focus refetch triggers, not just back-to-back server signals.
- **Dependency cutoff:** uv config sets `exclude-newer = "P14D"` (a moving 14-day window); choose an `sse-starlette` version that resolves within it against the pinned `starlette>=0.37`, and pin an explicit lower bound once chosen so resolution is stable over time.

## Design notes

Decisions captured during review iteration.

- **Round 1 — unify on refetch-on-connect.** Rather than serving "live update" and "stale tab refocus" with two client mechanisms, the stream emits an event on every (re)connect and the client refetches on any event. One code path; reconnect-after-sleep self-heals for free. `visibilitychange` kept only as belt-and-braces.
- **Round 1 — ships broadcast explicitly.** The change gate must account for `shipped` events directly (e.g. a `shipped` count on `WalkSummary`) rather than relying on the coincidental `updated` event from the tracker file edit, so ships can't silently stop refreshing if that coincidence ever breaks.
- **Round 1 — three correctness constraints promoted from implementation detail to requirements:** signal the broadcaster from the event loop after the threaded walk returns (asyncio structures aren't thread-safe); the commit-before-broadcast ordering is guaranteed by that structure; and the in-memory broadcaster assumes a single server process.
- **Round 1 — `/events` holds no DB connection** for its lifetime, preserving the per-request-connection / clean-shutdown invariant; and a connection-leak success criterion was added (client set empties after repeated connect/disconnect cycles).
- **Round 1 — test strategy kept dependency-free:** broadcaster unit test as primary coverage; bounded reads and explicit broadcaster/lifespan construction for endpoint tests instead of adding `asgi-lifespan` or `pytest-timeout`.
