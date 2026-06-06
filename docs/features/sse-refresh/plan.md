# sse-refresh

## Overview

Make the open inbox tab refresh itself when docs land, change, or ship. An in-process `Broadcaster` holds the connected SSE clients; the single serialised walk worker signals it — from the event loop, after the threaded walk commits — whenever a walk produced a net change. A new `/events` endpoint (via `sse-starlette`) streams a contentless "changed" message to each client, including one on connect. The inbox page opens an `EventSource` and, on any message, debounced, refetches a rendered inbox fragment and swaps the card region in place — preserving the active project filter. Two phases: server-side SSE first (curl-testable), then the live client wiring.

## Key technical decisions

1. **In-process Broadcaster, signalled from the walk worker on the event loop**
  A tiny fanout object holds one `asyncio.Queue` per connected client. It is created in the app lifespan and signalled by the walk worker. The signal is sent from the worker *coroutine* after `await asyncio.to_thread(...)` returns — never from inside the threaded walk, because `asyncio.Queue` is not thread-safe. Because `_run_walk` closes (commits) its DB connection before returning, the change is committed before it is broadcast, so any client that refetches sees the committed rows. The broadcaster is in-memory and assumes a single server process (which is how the app runs).
  ```python
  # web/broadcaster.py
  import asyncio


  class Broadcaster:
      """In-process SSE fan-out. One asyncio.Queue per connected client.

      Single-process only: a multi-worker deployment would leave clients on
      one worker deaf to changes detected on another.
      """

      def __init__(self) -> None:
          self._clients: set[asyncio.Queue[str]] = set()

      def register(self) -> asyncio.Queue[str]:
          q: asyncio.Queue[str] = asyncio.Queue()
          self._clients.add(q)
          return q

      def unregister(self, q: asyncio.Queue[str]) -> None:
          self._clients.discard(q)

      def broadcast(self, message: str = "changed") -> None:
          # maxsize=0 (unbounded) so put_nowait never raises. Messages are
          # contentless; a normally-disconnecting client is removed via the
          # endpoint's finally. (Repaints are coalesced client-side by the
          # debounce, not in the queue — a lagging client just holds N copies.)
          for q in self._clients:
              q.put_nowait(message)

      @property
      def client_count(self) -> int:
          return len(self._clients)
  ```
2. **Broadcaster created unconditionally; the worker is its only signaller**
  The lifespan creates `app.state.broadcaster` even when the DB or docs root isn't configured, so `/events` is uniform: it always has a broadcaster to register against. When discovery isn't configured there is simply no walk worker, so nothing ever calls `broadcast()` and the stream idles after its connect message (sse-starlette keepalives hold it open). The endpoint also reads `getattr(app.state, "broadcaster", None)` defensively so a bare `TestClient` (which doesn't run lifespan) degrades gracefully.
3. **Coarse default-message events; one client rule, refetch-on-connect**
  The stream emits unnamed SSE messages (default `message` event), so the client needs a single `onmessage` handler. One message is emitted immediately on connect — and therefore on every automatic `EventSource` reconnect — which unifies "new doc landed" and "stale tab re-syncs after sleep" into one rule: *on any message, debounced refetch*. The refetch (not reconnection) is what re-syncs stale content. `visibilitychange` is kept as belt-and-braces.
  ```python
  # web/events.py
  from sse_starlette.sse import EventSourceResponse
  from starlette.requests import Request

  from feature_skills_webapp.web.broadcaster import Broadcaster


  async def events(request: Request) -> EventSourceResponse:
      broadcaster: Broadcaster | None = getattr(request.app.state, "broadcaster", None)

      async def stream():
          # Refetch-on-connect: fires on first open AND every EventSource reconnect.
          yield {"data": "changed"}
          if broadcaster is None:
              return
          q = broadcaster.register()
          try:
              while True:
                  msg = await q.get()
                  yield {"data": msg}
          finally:
              broadcaster.unregister(q)   # deregister on disconnect — no leak

      # No DB connection is opened for the lifetime of this stream.
      return EventSourceResponse(stream())
  ```
  Client side (added to `index.html`), single handler + debounce, preserving the current project filter:
  ```javascript
  const es = new EventSource('/events');
  let timer = null;
  function scheduleRefetch() {
    clearTimeout(timer);
    timer = setTimeout(refetchInbox, 250);   // coalesce bursts + connect/focus triggers
  }
  async function refetchInbox() {
    const params = new URLSearchParams(window.location.search);
    params.set('fragment', '1');
    const resp = await fetch('/?' + params.toString());
    if (!resp.ok) return;
    document.getElementById('inbox-body').innerHTML = await resp.text();
  }
  es.onmessage = scheduleRefetch;                      // connect message + every change
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) scheduleRefetch();           // belt-and-braces
  });
  ```
  **Notes for the implementer:** the connect message fires `onmessage` immediately on first load, so the freshly server-rendered page does one redundant (idempotent, no-store) refetch ~250 ms later. This is *intended* — do not suppress the first message, or reconnect-after-sleep would stop self-healing. The single shared `timer` is deliberate so bursts across `onmessage` and `visibilitychange` coalesce into one refetch. Keepalive needs no configuration: rely on `EventSourceResponse`'s default ~15 s ping.
4. **Ship-aware change gate on WalkSummary**
  `WalkSummary` gains a `shipped` count and a `changed` property. The worker broadcasts only when `summary.changed` is true, so a no-op walk doesn't repaint. Ships broadcast on their own terms rather than relying on the tracker file's incidental `updated` event.
  ```python
  # storage/walker.py — WalkSummary
  @dataclass
  class WalkSummary:
      created: int = 0
      updated: int = 0
      archived: int = 0
      missing: int = 0
      reactivated: int = 0
      shipped: int = 0          # NEW
      errors: int = 0
      duration_ms: int = 0

      @property
      def changed(self) -> bool:
          return bool(
              self.created or self.updated or self.archived
              or self.missing or self.reactivated or self.shipped
          )

  # _apply_tracker_rows gains a `summary` arg; on the done-transition branch
  # (where the 'shipped' event is inserted) it does: summary.shipped += 1
  ```
5. **Inbox fragment via `?fragment=1` on the existing index route**
  The dynamic card region of `index.html` is extracted into a partial, `_inbox_body.html`, which the full page includes inside `<div id="inbox-body">`. The `index` handler, when `?fragment=1` is present, renders just the partial with the same context and the same `Cache-Control: no-store` — in both the configured and not-configured branches. The client always forwards the active `project` param, so live updates respect the current filter.
  ```python
  # web/routes.py — index(), sketch
  fragment = request.query_params.get("fragment")
  template = "_inbox_body.html" if fragment else "index.html"
  headers = {"Cache-Control": "no-store"}

  if app.state.db_path is None:
      return templates.TemplateResponse(request, template, {"configured": False}, headers=headers)
  # ... build inbox + projects as today ...
  return templates.TemplateResponse(request, template, {
      "configured": True, "inbox": inbox, "projects": projects, "active_project": project,
  }, headers=headers)
  ```
6. **Dependency-free tests: Broadcaster unit test is primary**
  The highest-value, lowest-flake coverage is unit-testing the `Broadcaster` directly (register / fan-out / unregister / leak), with no app, transport, or sockets — mirroring how `discovery_test.py` drives `_worker` with a `SimpleNamespace` app. For the wired endpoint, use `httpx.AsyncClient(transport=ASGITransport(app=app))`, assign `app.state.broadcaster` manually (bare ASGITransport doesn't run `lifespan`), and bound every stream read with `asyncio.wait_for` so a hung read can't stall an xdist worker. No `asgi-lifespan` or `pytest-timeout` added.
  ```python
  # events_test.py — bounded endpoint read, sketch
  import asyncio, httpx
  from httpx import ASGITransport
  from feature_skills_webapp.web.app import create_app
  from feature_skills_webapp.web.broadcaster import Broadcaster

  async def _read_until_data(resp, timeout=5) -> str:
      # sse-starlette may emit a leading ping/comment or blank lines; read until
      # an actual `data:` line. The whole loop is bounded so it can't hang xdist.
      async def _loop() -> str:
          async for line in resp.aiter_lines():
              if line.startswith("data:"):
                  return line
          raise AssertionError("stream ended before a data line")
      return await asyncio.wait_for(_loop(), timeout=timeout)

  async def test_events_emits_on_connect() -> None:
      app = create_app(db_path=None)
      app.state.broadcaster = Broadcaster()          # lifespan doesn't run under ASGITransport
      transport = ASGITransport(app=app)
      async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
          # Exit the stream block promptly — never await the infinite generator to completion.
          async with client.stream("GET", "/events") as resp:
              assert (await _read_until_data(resp)).startswith("data:")
  ```
  **Disconnect-detection fallback.** The deregister-on-disconnect assertion (`client_count` returns to 0 after the stream closes) depends on sse-starlette firing the generator's `finally` promptly under `ASGITransport`, which is less crisp than over a real socket. If it proves flaky, do not fight the transport: drop the transport-level assertion and instead drive the endpoint's generator directly (its `finally` calls `unregister`), relying on the `Broadcaster`-level leak test for the core guarantee.
7. **`sse-starlette` dependency, pinned within the uv cutoff**
  Add `sse-starlette` to `[project].dependencies`. It must resolve against the pinned `starlette>=0.37` and within uv's `exclude-newer = "P14D"` window; pick a concrete lower bound (e.g. `sse-starlette>=2.1`, confirmed by `uv lock`) so resolution stays stable as the moving window advances. sse-starlette supplies the correct `Cache-Control: no-cache` and no-buffering response headers for the stream.

## File structure

### New files

- `feature_skills_webapp/web/broadcaster.py` — the `Broadcaster` fan-out.
- `feature_skills_webapp/web/broadcaster_test.py` — register / fan-out / unregister / leak.
- `feature_skills_webapp/web/events.py` — the `/events` SSE endpoint.
- `feature_skills_webapp/web/events_test.py` — connect message, delivery, deregister-on-disconnect (bounded reads).
- `feature_skills_webapp/web/templates/_inbox_body.html` — the extracted inbox card-region partial.

### Modified files

- `pyproject.toml` — add `sse-starlette` dependency.
- `feature_skills_webapp/web/app.py` — create `app.state.broadcaster` in lifespan; add `Route("/events", events)`.
- `feature_skills_webapp/storage/walker.py` — `WalkSummary.shipped` + `.changed`; thread shipped count through `_apply_tracker_rows`.
- `feature_skills_webapp/storage/walker_test.py` — shipped count + `changed` tests.
- `feature_skills_webapp/web/discovery.py` — broadcast in `_worker` after the threaded walk when `summary.changed`.
- `feature_skills_webapp/web/discovery_test.py` — broadcast-on-change / no-broadcast-on-no-op.
- `feature_skills_webapp/web/routes.py` — `?fragment=1` handling on `index`.
- `feature_skills_webapp/web/templates/index.html` — wrap body in `#inbox-body`, include the partial, add the `EventSource` client.
- `feature_skills_webapp/web/routes_test.py` — fragment rendering / no-store / filter / full-page wiring.

## Phase 1 — Server-side SSE

### What's built

The `Broadcaster`, the `/events` endpoint, the lifespan wiring, the ship-aware `WalkSummary`, and the worker's broadcast call. After this phase, `curl -N localhost:8800/events` emits a message on connect and another whenever a doc is written into the dev-store and discovered.

### Files touched

New: `broadcaster.py`, `broadcaster_test.py`, `events.py`, `events_test.py`. Modified: `pyproject.toml`, `app.py`, `walker.py`, `walker_test.py`, `discovery.py`, `discovery_test.py`.

### Tests

- **Broadcaster**: `broadcast` fans the message out to every registered queue; `unregister` stops delivery; a register → unregister cycle repeated N times leaves `client_count == 0` (leak check); `broadcast` with no clients is a no-op.
- **WalkSummary**: a done-transition increments `shipped`; `changed` is true when any non-error counter is set and false for an empty summary.
- **/events endpoint**: a connect message arrives (bounded `wait_for`); a subsequent `broadcaster.broadcast()` is delivered; closing the stream deregisters the client (`client_count` returns to 0).
- **Worker**: with a real `Broadcaster` on `app.state` and a patched `_run_walk` returning `WalkSummary(created=1)`, a walk broadcasts (a registered queue receives a message); a `WalkSummary()` with all-zero counters does not broadcast.

### MR chain

One MR titled `feat(sse-refresh): phase 1 — server-side SSE`.

## Phase 2 — Live inbox

### What's built

The inbox fragment (partial + `?fragment=1` route handling) and the `EventSource` client that refetches and swaps the card region. After this phase, writing a doc in another terminal makes a card appear under "New since last visit" without a reload, and a tab whose stream dropped re-syncs on reconnect.

### Files touched

New: `templates/_inbox_body.html`. Modified: `routes.py`, `templates/index.html`, `routes_test.py`.

### Tests

- `GET /?fragment=1` returns body-only HTML (no `<html>`/`<head>`), status 200, with `Cache-Control: no-store`, containing the card markup.
- The fragment respects the `?project=` filter (only that project's cards appear).
- The not-configured fragment (`db_path=None`) returns the not-configured panel rather than erroring.
- The full page still renders and contains `id="inbox-body"` and the `EventSource` bootstrap script.

### MR chain

One MR titled `feat(sse-refresh): phase 2 — live inbox`, depends on Phase 1's `/events` endpoint.

## QC

There is no `CLAUDE.md` in this repo; follow the project's `README.md` § "Development" before each commit:

```bash
uv sync
uv run pytest
uv run ruff format . && uv run ruff check . && uv run ty check .
```

All must be clean. The suite runs under pytest-xdist + pytest-socket (`--disable-socket --allow-unix-socket`); in-process ASGI transport needs no real sockets, and every SSE stream read in tests must be bounded by `asyncio.wait_for`.

## Checklist

### Phase 1: Server-side SSE

- Add `sse-starlette` to `pyproject.toml` `[project].dependencies` with a concrete lower bound that resolves against `starlette>=0.37` and within `exclude-newer = "P14D"`; run `uv lock` / `uv sync`.
- Add `web/broadcaster.py` with `Broadcaster` (`register` / `unregister` / `broadcast` / `client_count`).
- Add `web/broadcaster_test.py`: fan-out to multiple queues, unregister stops delivery, N-cycle leak check (`client_count == 0`), no-client no-op.
- Add `shipped: int = 0` and the `changed` property to `WalkSummary`; thread a `summary` arg into `_apply_tracker_rows` (and update its call site in `_process_file` to pass `summary`) and increment `shipped` on the done-transition branch.
- Extend `walker_test.py`: a done-transition increments `shipped`; `changed` true for any non-error counter and false for an empty summary.
- Add `web/events.py`: `/events` via `EventSourceResponse` — yield a connect message, then stream the registered queue, deregistering in `finally`; defensive `getattr` for the broadcaster; open no DB connection.
- Add `web/events_test.py`: connect message arrives; a `broadcast()` is delivered; disconnect deregisters the client — all reads bounded by `asyncio.wait_for`, broadcaster assigned manually to `app.state`.
- Wire `app.py`: create `app.state.broadcaster = Broadcaster()` unconditionally in lifespan; add `Route("/events", events)`.
- In `discovery.py::_worker`, after `await asyncio.to_thread(...)` returns, call `broadcaster.broadcast()` when `summary.changed` (guarding `getattr(app.state, "broadcaster", None)`).
- Extend `discovery_test.py`: a change-producing walk broadcasts to a registered queue; a `shipped`-only summary (`WalkSummary(shipped=1)`) broadcasts (user story 2); an all-zero summary does not.
- QC (ruff format + check, ty, pytest) per README § Development; verify `curl -N localhost:8800/events` emits on connect and on a discovered change.

### Phase 2: Live inbox

- Extract the dynamic card region of `index.html` into `templates/_inbox_body.html` — the chips, the not-configured/empty states, and the three category sections all go *inside* the partial; the `<header class="page-header">` and the `pageshow` script stay in `index.html`, outside `#inbox-body`.
- In `index.html`, wrap the include as `<div id="inbox-body">{% include "_inbox_body.html" %}</div>`.
- Add `?fragment=1` handling to `routes.py::index`: render `_inbox_body.html` with the same context and `Cache-Control: no-store` in both configured and not-configured branches; preserve the `project` param.
- Add the `EventSource` client to `index.html` (alongside, not replacing, the existing `pageshow` handler): a single shared debounce `timer`; `onmessage` → 250 ms-debounced refetch of `/?fragment=1&project=<active>`, swap `#inbox-body`; `visibilitychange` refetch sharing the same timer.
- Extend `routes_test.py`: fragment returns body-only HTML + no-store + respects the project filter; not-configured fragment renders the panel; full page contains `#inbox-body` and the `EventSource` script.
- QC (ruff format + check, ty, pytest); manually verify a doc written in another terminal appears in the inbox without reload and a dropped stream re-syncs on reconnect.
