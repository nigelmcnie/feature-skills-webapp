"""Tests for web/events.py — generator-level testing (see plan decision 6).

ASGITransport buffers the entire response body and only returns when the ASGI
app completes; since an SSE stream never finishes, ASGITransport deadlocks.
We drive the generator directly instead: call events(), access body_iterator,
and step through it with asyncio.wait_for-bounded __anext__() calls.

The connect-message yield pauses the generator BEFORE q = broadcaster.register()
runs. We must advance past that point (task + sleep(0)) before broadcasting.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

from starlette.requests import Request

from feature_skills_webapp.web.broadcaster import Broadcaster
from feature_skills_webapp.web.events import events


def _make_request(broadcaster: Broadcaster | None = None) -> Request:
    """Minimal Request whose app.state.broadcaster is set appropriately."""
    state = SimpleNamespace()
    if broadcaster is not None:
        state.broadcaster = broadcaster
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/events",
        "query_string": b"",
        "headers": [],
        "app": SimpleNamespace(state=state),
    }
    return Request(scope)


def _gen(resp) -> AsyncGenerator[Any]:
    """Cast body_iterator to AsyncGenerator so the type checker knows about aclose/__anext__."""
    return resp.body_iterator  # type: ignore[return-value]


async def _next(gen: AsyncGenerator[Any], timeout: float = 5) -> Any:
    """Read the next event from the generator with a deadline."""
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


async def _advance_to_registered(gen: AsyncGenerator[Any]) -> asyncio.Task[Any]:
    """Start the next iteration so the generator runs past broadcaster.register().

    The first yield in the stream() generator pauses execution before
    q = broadcaster.register() runs. One sleep(0) after starting the task lets
    the generator run all the way to await q.get() (all steps are synchronous
    up to that point), so the client is registered when this returns.

    Returns the pending task. The caller must either complete it (by
    broadcasting a message) or cancel it before calling gen.aclose().
    """
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    return task


async def test_events_emits_connect_message() -> None:
    broadcaster = Broadcaster()
    resp = await events(_make_request(broadcaster))
    gen = _gen(resp)

    event = await _next(gen)
    assert event == {"data": "changed"}

    await gen.aclose()


async def test_events_delivers_broadcast() -> None:
    broadcaster = Broadcaster()
    resp = await events(_make_request(broadcaster))
    gen = _gen(resp)

    await _next(gen)  # connect message; generator not yet registered

    # advance past register() so the queue is live before we broadcast
    task = await _advance_to_registered(gen)
    broadcaster.broadcast("changed")

    event = await asyncio.wait_for(task, timeout=5)
    assert event == {"data": "changed"}

    await gen.aclose()


async def test_events_deregisters_on_disconnect() -> None:
    broadcaster = Broadcaster()
    resp = await events(_make_request(broadcaster))
    gen = _gen(resp)

    await _next(gen)  # connect message

    # advance past register() and confirm registration
    task = await _advance_to_registered(gen)
    assert broadcaster.client_count == 1

    # deliver a message so the pending task completes cleanly (no active __anext__ conflict)
    broadcaster.broadcast("msg")
    await asyncio.wait_for(task, timeout=5)

    # generator is now paused at the second yield inside the try block;
    # aclose() injects GeneratorExit there, triggering the finally → unregister
    await gen.aclose()
    assert broadcaster.client_count == 0


async def test_events_no_broadcaster_on_state() -> None:
    """When app.state has no broadcaster, the stream yields the connect message and ends."""
    resp = await events(_make_request(broadcaster=None))
    gen = _gen(resp)

    event = await _next(gen)
    assert event == {"data": "changed"}

    # generator exits after the connect message when there is no broadcaster
    try:
        await asyncio.wait_for(gen.__anext__(), timeout=1)
        raise AssertionError("expected StopAsyncIteration")
    except StopAsyncIteration:
        pass
