"""Unit tests for web/broadcaster.py."""

from __future__ import annotations

from feature_skills_webapp.web.broadcaster import Broadcaster


async def test_broadcast_fans_out_to_all_queues() -> None:
    b = Broadcaster()
    q1 = b.register()
    q2 = b.register()
    b.broadcast("changed")
    assert q1.get_nowait() == "changed"
    assert q2.get_nowait() == "changed"


async def test_unregister_stops_delivery() -> None:
    b = Broadcaster()
    q = b.register()
    b.unregister(q)
    b.broadcast("changed")
    assert q.empty()


async def test_register_unregister_cycle_no_leak() -> None:
    b = Broadcaster()
    for _ in range(10):
        q = b.register()
        b.unregister(q)
    assert b.client_count == 0


async def test_broadcast_no_clients_is_noop() -> None:
    b = Broadcaster()
    b.broadcast("changed")  # must not raise


async def test_client_count() -> None:
    b = Broadcaster()
    q1 = b.register()
    q2 = b.register()
    assert b.client_count == 2
    b.unregister(q1)
    assert b.client_count == 1
    b.unregister(q2)
    assert b.client_count == 0
