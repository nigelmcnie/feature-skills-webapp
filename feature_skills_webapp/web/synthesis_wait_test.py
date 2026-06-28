"""Tests for the synthesis/wait endpoint (get_document_synthesis_wait) in web/submit.py.

Blocking paths are driven as direct coroutines (task + sleep(0)) — the same
pattern used in events_test.py — since TestClient is synchronous and would
deadlock waiting for a held connection to resolve.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

import feature_skills_webapp.web.submit as submit_mod
from feature_skills_webapp.web.app import create_app
from feature_skills_webapp.web.broadcaster import Broadcaster
from feature_skills_webapp.web.submit import get_document_synthesis_wait

_PROJECT = "proj"
_FEATURE = "feat-a"
_DOC_TYPE = "plan"
_INSTANCE = 1
_LKEY = f"{_PROJECT}/{_FEATURE}/{_DOC_TYPE}/{_INSTANCE}"

_WAIT_URL = f"/api/documents/{_PROJECT}/{_FEATURE}/{_DOC_TYPE}/{_INSTANCE}/synthesis/wait"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_doc(client: TestClient) -> int:
    """Create project + feature + plan document; return document_id."""
    client.post(f"/api/projects/{_PROJECT}")
    client.post(f"/api/projects/{_PROJECT}/features/{_FEATURE}", json={})
    resp = client.put(
        f"/api/documents/{_PROJECT}/{_FEATURE}/{_DOC_TYPE}/{_INSTANCE}",
        json={"sections": {"overview": "<p>Test plan.</p>"}},
    )
    assert resp.status_code == 200
    return int(resp.json()["document_id"])


def _post_synthesis(client: TestClient, doc_id: int) -> None:
    """Submit synthesis responses for a document."""
    resp = client.post(
        f"/doc/{doc_id}/synthesis-response",
        json={"responses": {"1": "an answer"}, "routine_flags": {"2": "routine note"}},
    )
    assert resp.status_code == 200


def _make_wait_request(
    db_path: Path | None,
    broadcaster: Broadcaster | None,
) -> Request:
    """Build a minimal Starlette Request for the wait handler."""
    state = SimpleNamespace(db_path=db_path)
    if broadcaster is not None:
        state.broadcaster = broadcaster
    scope = {
        "type": "http",
        "method": "GET",
        "path": _WAIT_URL,
        "query_string": b"",
        "headers": [],
        "path_params": {
            "project": _PROJECT,
            "feature": _FEATURE,
            "doc_type": _DOC_TYPE,
            "instance": _INSTANCE,
        },
        "app": SimpleNamespace(state=state),
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# Non-blocking paths (TestClient)
# ---------------------------------------------------------------------------


def test_already_submitted_returns_immediately(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = _seed_doc(client)
        _post_synthesis(client, doc_id)
        resp = client.get(_WAIT_URL)

    assert resp.status_code == 200
    data = resp.json()
    assert data["doc"] == _LKEY
    assert data["submitted"] is True
    assert data["responses"] == {"1": "an answer"}
    assert data["routine_flags"] == {"2": "routine note"}


def test_503_db_not_configured() -> None:
    client = TestClient(create_app(db_path=None))
    resp = client.get(_WAIT_URL)
    assert resp.status_code == 503
    assert "db not configured" in resp.json()["error"]


def test_404_unknown_logical_key(temp_db: Path) -> None:
    client = TestClient(create_app(db_path=temp_db))
    resp = client.get(_WAIT_URL)
    assert resp.status_code == 404
    assert "document not found" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Missing broadcaster — degrades immediately to submitted=false
# ---------------------------------------------------------------------------


async def test_missing_broadcaster_returns_immediately(temp_db: Path) -> None:
    with TestClient(create_app(db_path=temp_db)) as client:
        _seed_doc(client)  # no synthesis

    request = _make_wait_request(temp_db, broadcaster=None)
    result = await get_document_synthesis_wait(request)

    assert result.status_code == 200
    data = json.loads(bytes(result.body))
    assert data["submitted"] is False
    assert data["doc"] == _LKEY


# ---------------------------------------------------------------------------
# Timeout — tiny wait_timeout, no synthesis → returns submitted=false
# ---------------------------------------------------------------------------


async def test_timeout_returns_submitted_false(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(submit_mod, "wait_timeout", lambda: 0.01)

    broadcaster = Broadcaster()
    with TestClient(create_app(db_path=temp_db)) as client:
        _seed_doc(client)  # no synthesis

    request = _make_wait_request(temp_db, broadcaster)
    result = await asyncio.wait_for(get_document_synthesis_wait(request), timeout=5)

    assert result.status_code == 200
    data = json.loads(bytes(result.body))
    assert data["submitted"] is False
    assert data["doc"] == _LKEY
    # Full contract row: the timeout payload carries empty response maps.
    assert data["responses"] == {}
    assert data["routine_flags"] == {}


# ---------------------------------------------------------------------------
# Wake-on-submit — a submission during the wait wakes the handler
# ---------------------------------------------------------------------------


async def test_wake_on_submit(temp_db: Path) -> None:
    broadcaster = Broadcaster()
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = _seed_doc(client)  # no synthesis

    request = _make_wait_request(temp_db, broadcaster)

    task = asyncio.ensure_future(get_document_synthesis_wait(request))
    await asyncio.sleep(0)  # let handler register and reach q.get()

    # Insert synthesis directly and broadcast — simulates a submission
    from feature_skills_webapp.storage.db import connect, now_iso, transaction

    conn = connect(temp_db)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO synthesis_responses "
                "(document_id, item_num, response, routine_flag, updated_at) "
                "VALUES (?, 1, 'woken answer', NULL, ?)",
                (doc_id, now_iso()),
            )
    finally:
        conn.close()

    broadcaster.broadcast()

    result = await asyncio.wait_for(task, timeout=5)
    assert result.status_code == 200
    data = json.loads(bytes(result.body))
    assert data["submitted"] is True
    assert data["responses"] == {"1": "woken answer"}


# ---------------------------------------------------------------------------
# Coarse-signal re-check — unrelated broadcast doesn't satisfy the wait
# ---------------------------------------------------------------------------


async def test_coarse_signal_recheck(temp_db: Path) -> None:
    """A broadcast for an unrelated change re-checks but doesn't return early."""
    broadcaster = Broadcaster()
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = _seed_doc(client)  # no synthesis

    request = _make_wait_request(temp_db, broadcaster)

    task = asyncio.ensure_future(get_document_synthesis_wait(request))
    await asyncio.sleep(0)  # handler registered, waiting on q

    # Coarse signal — unrelated change (no synthesis for this doc). Assert the
    # wait does NOT return by proving it stays pending across a wall-clock
    # window, rather than counting scheduler turns with sleep(0): the turn count
    # depends on event-loop scheduling order, which is exactly the
    # non-determinism TESTING.md warns against. shield() keeps the handler alive
    # past the wait_for timeout, so the TimeoutError *is* the assertion that the
    # coarse broadcast did not end the wait.
    broadcaster.broadcast()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
    assert not task.done(), "handler returned early on a coarse signal"

    # Now actually submit
    from feature_skills_webapp.storage.db import connect, now_iso, transaction

    conn = connect(temp_db)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO synthesis_responses "
                "(document_id, item_num, response, routine_flag, updated_at) "
                "VALUES (?, 1, 'final answer', NULL, ?)",
                (doc_id, now_iso()),
            )
    finally:
        conn.close()

    broadcaster.broadcast()

    result = await asyncio.wait_for(task, timeout=5)
    assert result.status_code == 200
    data = json.loads(bytes(result.body))
    assert data["submitted"] is True


# ---------------------------------------------------------------------------
# Cleanup: client_count == 0 after submit, timeout, and cancel
# ---------------------------------------------------------------------------


async def test_cleanup_after_submit(temp_db: Path) -> None:
    broadcaster = Broadcaster()
    with TestClient(create_app(db_path=temp_db)) as client:
        doc_id = _seed_doc(client)

    request = _make_wait_request(temp_db, broadcaster)
    task = asyncio.ensure_future(get_document_synthesis_wait(request))
    await asyncio.sleep(0)
    assert broadcaster.client_count == 1

    from feature_skills_webapp.storage.db import connect, now_iso, transaction

    conn = connect(temp_db)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO synthesis_responses "
                "(document_id, item_num, response, routine_flag, updated_at) "
                "VALUES (?, 1, 'done', NULL, ?)",
                (doc_id, now_iso()),
            )
    finally:
        conn.close()

    broadcaster.broadcast()
    await asyncio.wait_for(task, timeout=5)

    assert broadcaster.client_count == 0


async def test_cleanup_after_timeout(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(submit_mod, "wait_timeout", lambda: 0.01)

    broadcaster = Broadcaster()
    with TestClient(create_app(db_path=temp_db)) as client:
        _seed_doc(client)

    request = _make_wait_request(temp_db, broadcaster)
    task = asyncio.ensure_future(get_document_synthesis_wait(request))
    await asyncio.sleep(0)
    assert broadcaster.client_count == 1

    await asyncio.wait_for(task, timeout=5)
    assert broadcaster.client_count == 0


async def test_cleanup_after_cancel(temp_db: Path) -> None:
    broadcaster = Broadcaster()
    with TestClient(create_app(db_path=temp_db)) as client:
        _seed_doc(client)

    request = _make_wait_request(temp_db, broadcaster)
    task = asyncio.ensure_future(get_document_synthesis_wait(request))
    await asyncio.sleep(0)
    assert broadcaster.client_count == 1

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert broadcaster.client_count == 0


# ---------------------------------------------------------------------------
# Read-error propagation — error surfaces and queue is still unregistered
# ---------------------------------------------------------------------------


async def test_read_error_propagates_and_unregisters(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broadcaster = Broadcaster()
    with TestClient(create_app(db_path=temp_db)) as client:
        _seed_doc(client)

    def _broken(conn: object, lkey: str) -> None:
        raise RuntimeError("simulated DB error")

    monkeypatch.setattr(submit_mod, "_read_synthesis_state", _broken)

    request = _make_wait_request(temp_db, broadcaster)
    task = asyncio.ensure_future(get_document_synthesis_wait(request))
    await asyncio.sleep(0)  # handler runs synchronously up to the error

    assert task.done()
    assert broadcaster.client_count == 0
    with pytest.raises(RuntimeError, match="simulated DB error"):
        await task
