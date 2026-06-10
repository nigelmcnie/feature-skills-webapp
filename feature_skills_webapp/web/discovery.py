"""Walk worker and request_walk helper for doc-discovery."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watchfiles import awatch

from feature_skills_webapp.storage.db import connect
from feature_skills_webapp.storage.walker import WalkSummary, walk

log = logging.getLogger(__name__)

# Backoff between watch restarts, so a watch that fails immediately and repeatedly
# can't hot-loop. Module-level so tests can shrink it.
_WATCH_RETRY_DELAY_S = 2.0


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
    """True for .html files that should be indexed; false for dotfiles/editor-temp/non-HTML.

    Judges the path *relative to the docs root* — its dot-component check would
    otherwise reject everything when the store itself sits under a dotted dir
    (e.g. ``~/.claude/feature-docs``). Callers handed an absolute path must
    relativise first; see ``_is_indexable``.
    """
    if path.suffix != ".html":
        return False
    return not any(part.startswith(".") and part != ".feedback-archive" for part in path.parts)


def _is_indexable(root: Path, abs_path: str) -> bool:
    """``should_index`` for an absolute path that ``awatch`` reports under ``root``.

    awatch yields absolute paths, but ``should_index``'s dot-component rule is
    about the store-relative path — and the store commonly lives under a dotted
    dir (``~/.claude/…``), whose leading dot would otherwise make every change
    look un-indexable. Relativise before judging, mirroring the walker.
    """
    path = Path(abs_path)
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return should_index(rel)


async def _watch(app: Any) -> None:
    """Supervise the filesystem watch so a crash can't silently stop auto-indexing.

    ``_watch`` is a fire-and-forget task whose result is held for the app's
    lifetime, so an unhandled exception in ``awatch`` dies *unlogged* — asyncio
    never surfaces the "exception never retrieved" warning while the reference is
    live — and auto-indexing just stops, leaving the inbox quietly stale until
    something else triggers a walk. So wrap the watch in a supervising loop that
    logs and restarts on any failure bar cancellation, and reconcile on each
    *re*start to catch changes missed while it was down (the initial reconcile is
    already kicked from the lifespan, so the first pass skips it).
    """
    first = True
    while True:
        try:
            if not first:
                await request_walk(app, reconcile=True, await_result=False)
            first = False
            root = Path(app.state.docs_root)
            async for changes in awatch(app.state.docs_root):
                if any(_is_indexable(root, p) for _change, p in changes):
                    await request_walk(app, reconcile=False, await_result=False)
            # awatch yields forever in normal operation; a clean return is itself
            # a fault, so treat it the same as a crash and restart.
            log.warning(
                "file watch stopped unexpectedly; restarting in %.1fs", _WATCH_RETRY_DELAY_S
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("file watch crashed; restarting in %.1fs", _WATCH_RETRY_DELAY_S)
        await asyncio.sleep(_WATCH_RETRY_DELAY_S)
