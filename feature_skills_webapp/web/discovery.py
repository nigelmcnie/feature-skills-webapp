"""Walk worker and request_walk helper for doc-discovery."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from feature_skills_webapp.storage.db import connect
from feature_skills_webapp.storage.walker import WalkSummary, walk

log = logging.getLogger(__name__)


@dataclass
class WalkRequest:
    reconcile: bool
    future: asyncio.Future[WalkSummary] | None  # resolved by the worker with WalkSummary


def _run_walk(db_path: Path, docs_root: Path, reconcile: bool) -> WalkSummary:
    """Runs in a worker thread: its own connection (sqlite conns aren't shareable across threads)."""
    conn = connect(db_path)
    try:
        return walk(conn, docs_root, reconcile=reconcile)
    finally:
        conn.close()


async def _worker(app: Any) -> None:
    q: asyncio.Queue[WalkRequest] = app.state.walk_queue
    batch: list[WalkRequest] = []
    try:
        while True:
            batch = [await q.get()]
            while not q.empty():
                batch.append(q.get_nowait())
            reconcile = any(r.reconcile for r in batch)
            try:
                summary = await asyncio.to_thread(
                    _run_walk,
                    app.state.db_path,
                    app.state.docs_root,
                    reconcile,
                )
            except Exception:
                log.exception("walk failed")
                summary = WalkSummary(errors=1)
            if summary.changed:
                broadcaster = getattr(app.state, "broadcaster", None)
                if broadcaster is not None:
                    broadcaster.broadcast()
            for r in batch:
                if r.future and not r.future.done():
                    r.future.set_result(summary)
            batch = []
    except asyncio.CancelledError:
        # Resolve every outstanding future so no awaiting caller hangs on shutdown —
        # both the current batch and any requests still queued but not yet batched.
        pending = list(batch)
        while not q.empty():
            try:
                pending.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        for r in pending:
            if r.future and not r.future.done():
                r.future.set_result(WalkSummary(errors=1))
        raise


async def request_walk(app: Any, *, reconcile: bool, await_result: bool) -> WalkSummary | None:
    fut = asyncio.get_running_loop().create_future() if await_result else None
    await app.state.walk_queue.put(WalkRequest(reconcile, fut))
    if fut:
        return await asyncio.wait_for(fut, timeout=30)
    return None


def should_index(path: Path) -> bool:
    """True for .html files that should be indexed; false for dotfiles/editor-temp/non-HTML."""
    if path.suffix != ".html":
        return False
    return not any(part.startswith(".") and part != ".feedback-archive" for part in path.parts)


async def _watch(app: Any) -> None:
    from watchfiles import awatch

    async for changes in awatch(app.state.docs_root):
        if any(should_index(Path(p)) for _change, p in changes):
            await request_walk(app, reconcile=False, await_result=False)
