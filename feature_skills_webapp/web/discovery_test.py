"""Tests for web/discovery.py — deterministic via patching _run_walk."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from feature_skills_webapp.storage.walker import WalkSummary
from feature_skills_webapp.web.discovery import WalkRequest, _worker, request_walk, should_index


def make_app(db_path: Path, docs_root: Path) -> SimpleNamespace:
    app = SimpleNamespace()
    app.state = SimpleNamespace()
    app.state.db_path = db_path
    app.state.docs_root = docs_root
    app.state.walk_queue = asyncio.Queue()
    return app


async def test_request_walk_returns_summary(tmp_path: Path) -> None:
    app = make_app(tmp_path / "db.sqlite", tmp_path)
    summary_fixture = WalkSummary(created=3)

    with patch("feature_skills_webapp.web.discovery._run_walk", return_value=summary_fixture):
        worker_task = asyncio.create_task(_worker(app))
        result = await request_walk(app, reconcile=False, await_result=True)
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)

    assert result is not None
    assert result.created == 3


async def test_coalescing_two_requests_into_one_walk(tmp_path: Path) -> None:
    """Two requests enqueued during an in-flight walk coalesce into one follow-up walk."""
    app = make_app(tmp_path / "db.sqlite", tmp_path)

    walk_started = asyncio.Event()
    walk_proceed = asyncio.Event()
    call_count = 0
    loop = asyncio.get_running_loop()

    def blocking_run_walk(db_path: Path, docs_root: Path, reconcile: bool) -> WalkSummary:
        nonlocal call_count
        call_count += 1
        loop.call_soon_threadsafe(walk_started.set)
        import time

        while not walk_proceed.is_set():
            time.sleep(0.005)
        return WalkSummary(created=call_count)

    with patch("feature_skills_webapp.web.discovery._run_walk", side_effect=blocking_run_walk):
        worker_task = asyncio.create_task(_worker(app))

        # Enqueue first request — it starts the first walk
        fut1 = asyncio.get_running_loop().create_future()
        await app.state.walk_queue.put(WalkRequest(reconcile=False, future=fut1))

        # Wait for first walk to actually start
        await asyncio.wait_for(walk_started.wait(), timeout=5)

        # Enqueue two more requests while first walk is in-flight
        fut2 = asyncio.get_running_loop().create_future()
        fut3 = asyncio.get_running_loop().create_future()
        await app.state.walk_queue.put(WalkRequest(reconcile=False, future=fut2))
        await app.state.walk_queue.put(WalkRequest(reconcile=False, future=fut3))

        # Release the first walk
        walk_proceed.set()

        # Wait for all futures to resolve
        await asyncio.wait_for(asyncio.gather(fut1, fut2, fut3), timeout=10)

        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)

    # Should be exactly 2 walks: the first (in-flight) + one coalesced follow-up
    assert call_count == 2, f"expected 2 walks, got {call_count}"


async def test_on_demand_future_resolves_from_own_walk(tmp_path: Path) -> None:
    """The future from an await_result=True request is resolved by a walk started after enqueue."""
    app = make_app(tmp_path / "db.sqlite", tmp_path)
    summaries = [WalkSummary(created=1), WalkSummary(created=2)]
    call_count = 0

    def counting_run_walk(db_path: Path, docs_root: Path, reconcile: bool) -> WalkSummary:
        nonlocal call_count
        result = summaries[call_count]
        call_count += 1
        return result

    with patch("feature_skills_webapp.web.discovery._run_walk", side_effect=counting_run_walk):
        worker_task = asyncio.create_task(_worker(app))
        result = await request_walk(app, reconcile=False, await_result=True)
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)

    # The future is resolved by the walk started after enqueue (first walk)
    assert result is not None
    assert result.created == 1


async def test_worker_survives_failing_walk(tmp_path: Path) -> None:
    """A walk that raises doesn't crash the worker; summary reports errors=1."""
    app = make_app(tmp_path / "db.sqlite", tmp_path)
    call_count = 0

    def failing_then_ok(db_path: Path, docs_root: Path, reconcile: bool) -> WalkSummary:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("walk exploded")
        return WalkSummary(created=5)

    with patch("feature_skills_webapp.web.discovery._run_walk", side_effect=failing_then_ok):
        worker_task = asyncio.create_task(_worker(app))

        # First request — should get errors=1 summary
        result1 = await request_walk(app, reconcile=False, await_result=True)

        # Second request — worker must still be alive
        result2 = await request_walk(app, reconcile=False, await_result=True)

        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)

    assert result1 is not None
    assert result1.errors == 1
    assert result2 is not None
    assert result2.created == 5


async def test_cancelled_worker_resolves_outstanding_futures(tmp_path: Path) -> None:
    """On cancellation, the worker resolves any in-batch futures so requests don't hang."""
    app = make_app(tmp_path / "db.sqlite", tmp_path)

    start_event = asyncio.Event()
    block_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocking_run_walk(db_path: Path, docs_root: Path, reconcile: bool) -> WalkSummary:
        import time

        loop.call_soon_threadsafe(start_event.set)
        while not block_event.is_set():
            time.sleep(0.005)
        return WalkSummary()

    with patch("feature_skills_webapp.web.discovery._run_walk", side_effect=blocking_run_walk):
        worker_task = asyncio.create_task(_worker(app))
        fut = asyncio.get_running_loop().create_future()
        await app.state.walk_queue.put(WalkRequest(reconcile=False, future=fut))

        await asyncio.wait_for(start_event.wait(), timeout=5)

        # Cancel the worker while it's in-flight
        worker_task.cancel()
        block_event.set()
        await asyncio.wait_for(asyncio.gather(worker_task, return_exceptions=True), timeout=5)

    # The future should be resolved (with errors=1 fallback), not left pending
    assert fut.done()


async def test_cancelled_worker_resolves_queued_unbatched_futures(tmp_path: Path) -> None:
    """On cancellation, the worker also drains and resolves requests still queued
    but not yet pulled into the current batch — they must not hang."""
    app = make_app(tmp_path / "db.sqlite", tmp_path)

    start_event = asyncio.Event()
    block_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocking_run_walk(db_path: Path, docs_root: Path, reconcile: bool) -> WalkSummary:
        import time

        loop.call_soon_threadsafe(start_event.set)
        while not block_event.is_set():
            time.sleep(0.005)
        return WalkSummary()

    with patch("feature_skills_webapp.web.discovery._run_walk", side_effect=blocking_run_walk):
        worker_task = asyncio.create_task(_worker(app))

        # First request is pulled into the batch and starts the (blocking) walk.
        batched = loop.create_future()
        await app.state.walk_queue.put(WalkRequest(reconcile=False, future=batched))
        await asyncio.wait_for(start_event.wait(), timeout=5)

        # Second request arrives while the walk is in flight — it sits in the queue,
        # not yet batched.
        queued = loop.create_future()
        await app.state.walk_queue.put(WalkRequest(reconcile=False, future=queued))

        worker_task.cancel()
        block_event.set()
        await asyncio.wait_for(asyncio.gather(worker_task, return_exceptions=True), timeout=5)

    assert batched.done(), "in-batch future must be resolved on cancel"
    assert queued.done(), "queued-but-unbatched future must be resolved on cancel"
    assert queued.result().errors == 1


# --- should_index ---


def test_should_index_html_is_true():
    assert should_index(Path("proj/feat/context.html")) is True


def test_should_index_feedback_archive_html_is_true():
    assert should_index(Path("proj/feat/.feedback-archive/old.html")) is True


def test_should_index_swp_is_false():
    assert should_index(Path("proj/feat/context.html.swp")) is False


def test_should_index_tilde_backup_is_false():
    assert should_index(Path("proj/feat/context.html~")) is False


def test_should_index_non_html_is_false():
    assert should_index(Path("proj/feat/readme.md")) is False


def test_should_index_dotfile_dir_is_false():
    assert should_index(Path("proj/feat/.hidden/secret.html")) is False


def test_should_index_dotfile_at_root_is_false():
    assert should_index(Path(".gitignore")) is False
