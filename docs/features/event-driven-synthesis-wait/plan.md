# event-driven-synthesis-wait — Plan

## Overview

Add an event-driven `GET .../synthesis/wait` endpoint to the webapp so the `feature-requirements` skill can replace its five-second busy-poll with a single held call that wakes the instant the human submits (or returns cleanly after a bounded timeout). Phase 1 ships the endpoint and its tests in this repo; Phase 2 switches the shared `docs/webapp-polling.md` convention and the `feature-requirements` skill over to it, in the feature-skills repo. No schema change — the wait only reads existing synthesis state.

## Key decisions

1. **Register on the broadcaster *before* the first state read; loop on one queue**
  The handler registers a single broadcaster queue up front, then loops: read state, return if submitted, else block on the queue until woken or the deadline passes. Registering before the authoritative read is what makes it race-free — any submission that commits after registration is buffered in the queue (it is unbounded; `put_nowait` never drops). One registration is reused across all wakes; a single `try/finally` unregisters it on every exit path. **The `request_conn` must be opened fresh inside the loop on every iteration** — a new SQLite connection always reads at the latest committed WAL state, so the re-read after a wake sees the just-committed submission. Do *not* hoist the connection above the loop: a single long-lived read connection can pin an older snapshot and the wait would spin to timeout despite a real submission.
  ```
  q = broadcaster.register() if broadcaster else None
  try:
      loop = asyncio.get_running_loop()
      deadline = loop.time() + timeout
      while True:
          with request_conn(request.app) as conn:        # fresh connection each iteration (load-bearing)
              state = _read_synthesis_state(conn, lkey)   # None if doc missing
          if state is None:
              return JSONResponse({"error": "document not found"}, status_code=404)
          if state.submitted:
              return JSONResponse(state.as_payload())      # same shape as GET .../synthesis
          if q is None:
              return JSONResponse(state.as_payload())      # no broadcaster: degrade (submitted=false)
          remaining = deadline - loop.time()
          if remaining <= 0:
              return JSONResponse(state.as_payload())      # timeout: submitted=false
          try:
              await asyncio.wait_for(q.get(), timeout=remaining)
          except TimeoutError:
              return JSONResponse(state.as_payload())
  finally:
      if q is not None:
          broadcaster.unregister(q)
  ```
2. **One overall deadline, not a per-wake timeout**
  The bound is a single monotonic deadline (`loop.time() + timeout`), and `remaining` is recomputed before each `wait_for`. This way unrelated `changed` broadcasts (the signal is coarse — it fires on *any* committed change) wake the loop to re-check but never extend the total hold beyond the timeout.
3. **Shared read helper, reused by both endpoints**
  Extract the logical-key lookup + `synthesis_responses` query out of `get_document_synthesis` into a helper used by both the existing read and the new wait, so the response shape has one source of truth.
  ```
  @dataclass
  class SynthesisState:
      doc_id: int
      submitted: bool
      responses: dict[str, str]
      routine_flags: dict[str, str]
      def as_payload(self) -> dict: ...   # {"doc": lkey, "submitted", "responses", "routine_flags"}

  def _read_synthesis_state(conn, lkey: str) -> SynthesisState | None:
      # None when the document row is absent (→ 404)
  ```
4. **Timeout is a config accessor with an env override, default 240 s**
  Add `config.wait_timeout()` reading `FEATURE_SKILLS_WEBAPP_WAIT_TIMEOUT` (float seconds, default `240.0`), mirroring the existing `config.port()` pattern. 240 s sits just under the 5-minute Anthropic prompt-cache TTL, so a client that reconnects after each hold stays cache-warm while cutting reconnects roughly 10× versus the original 25 s. Because the hold is event-driven (a real submission wakes the held connection instantly via the broadcaster), a longer hold has zero responsiveness cost. There is no reverse proxy in front of the server today, and uvicorn sets no active-request timeout, so nothing collides with the longer hold; a *future* reverse proxy must set `proxy_read_timeout` ≥ the hold. If the hold is ever refused, the client already degrades to a 5 s short poll, so it fails safe. The value is recomputed per call so tests can set a tiny value via the env var or by monkeypatching the accessor.
5. **Errors propagate; the skill falls back to polling**
  No special handling for a transient DB read failure mid-wait: it propagates as a 500, which the skill treats as a failed wait and degrades to the existing short poll. The `finally` still unregisters the queue. The only deliberately-handled "can't wait" cases are `db_path is None` (503) and a missing broadcaster (immediate `submitted=false`).
6. **Test the blocking paths by driving the coroutine directly**
  A `TestClient` request runs to completion synchronously, so it suits the non-blocking cases (already-submitted, 404, 503, missing-broadcaster with a tiny timeout). The blocking cases (wake-on-submit, coarse-signal re-check, timeout, unregister-on-cancel) are driven exactly like `web/events_test.py`: build a real app, set `app.state`, call the handler in an `asyncio` task, `sleep(0)` to let it register and reach `q.get()`, then `broadcast()` or cancel and assert on `broadcaster.client_count`.

## Data model

No schema change. The wait endpoint reads the existing `documents` (by `logical_key`) and `synthesis_responses` (by `document_id`) tables — the same reads `get_document_synthesis` already performs. Nothing is written.

## Contract

### New endpoint

`GET /api/documents/{project}/{feature}/{doc_type}/{instance}/synthesis/wait`

Returns the **identical JSON shape** to the existing `GET .../synthesis`, so it is a drop-in replacement for the poll:

| Situation | Status | Body |
|---|---|---|
| Already submitted, or submitted during the wait | 200 | `{"doc", "submitted": true, "responses", "routine_flags"}` |
| Timeout elapsed with no submission | 200 | `{"doc", "submitted": false, "responses": {}, "routine_flags": {}}` |
| No broadcaster on app state (degrade) | 200 | `{"submitted": false, …}` immediately |
| Document not found | 404 | `{"error": "document not found"}` |
| DB not configured | 503 | `{"error": "db not configured"}` |

## File structure

### feature-skills-webapp (Phase 1)

- `feature_skills_webapp/config.py` — add `wait_timeout()`.
- `feature_skills_webapp/web/submit.py` — add `get_document_synthesis_wait`; extract `SynthesisState` + `_read_synthesis_state` and refactor `get_document_synthesis` to use it.
- `feature_skills_webapp/web/app.py` — register the new `.../synthesis/wait` route and import the handler.
- `feature_skills_webapp/web/synthesis_wait_test.py` — new test module (TestClient + direct-coroutine).
- `feature_skills_webapp/config_test.py` (if present) — cover `wait_timeout()` default + override.

### feature-skills (Phase 2, separate repo at ~/src/nigelmcnie/feature-skills)

- `docs/webapp-polling.md` — document the wait protocol; remove the "still waiting… every 60 s" instruction (≈line 26); drop the clipboard reference, replace with short-poll degradation.
- `feature-requirements/SKILL.md` — replace the Step 6 "Poll … every 5 seconds" loop and the 60 s status line with the single wait call + silent reconnect + poll fallback.

## Verification

### Phase 1 (run in ~/src/nigelmcnie/feature-skills-webapp)

```
# Full QC gate (all must pass) — per CLAUDE.md
uv run ruff format --check .
uv run ruff check .
uv run ty check .
uv run pytest                       # full suite (xdist + pytest-socket; per-worker DB)

# Focused: the new endpoint's battery alone
uv run pytest feature_skills_webapp/web/synthesis_wait_test.py -q

# Regression guard: the refactored existing read endpoint is unchanged
uv run pytest feature_skills_webapp/web/synthesis_test.py -q

# Route is wired (fails loudly if the endpoint is absent)
uv run python -c "from feature_skills_webapp.web.app import create_app; \
paths=[getattr(r,'path','') for r in create_app(db_path=None).routes]; \
assert any(p.endswith('/synthesis/wait') for p in paths), 'wait route missing'; print('route OK')"
```

### Phase 2 (run in ~/src/nigelmcnie/feature-skills)

```
# The wait protocol is documented and the 60s busy-poll instruction is gone
grep -q 'synthesis/wait' docs/webapp-polling.md && echo 'wait documented'
! grep -qi 'every 60' docs/webapp-polling.md && echo '60s line removed'
# The requirements skill references the wait endpoint
grep -q 'synthesis/wait' feature-requirements/SKILL.md && echo 'skill switched'
```

(No live-credential or external-service steps; everything runs locally.)

## Qc

Follow whatever `CLAUDE.md` specifies at implementation time. For feature-skills-webapp that is, before committing: `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, and `uv run pytest` — all must pass. After Phase 1 merges, restart the deployed service per CLAUDE.md (`systemctl --user restart feature-skills-webapp`) so the running inbox serves the new endpoint; no dependency change, so no reinstall needed.

## Checklist

### Phase 1: Wait endpoint (feature-skills-webapp)

- Write `synthesis_wait_test.py` with the full battery (already-submitted, wake-on-submit, coarse-signal re-check, timeout, cleanup on submit/timeout/cancel, missing-broadcaster, 404, 503, read-error propagation) — confirm each fails without the endpoint.
- Add `config.wait_timeout()` (env `FEATURE_SKILLS_WEBAPP_WAIT_TIMEOUT`, float, default 25.0) with default + override coverage.
- Extract `SynthesisState` + `_read_synthesis_state(conn, lkey)` in `web/submit.py` and refactor `get_document_synthesis` to use it — preserve the exact payload shape; run `synthesis_test.py` green both before and after the extraction to prove the live endpoint is unchanged.
- Implement `get_document_synthesis_wait`: register-then-check, one overall deadline, `try/finally` unregister, no-broadcaster degrade, 503/404 guards.
- Register the `.../synthesis/wait` route in `web/app.py` and import the handler.
- Run the full QC gate (ruff format/check, ty, pytest) and the route-presence check; all green.
- Open the Phase 1 MR.

### Phase 2: Skill adoption (feature-skills)

- Update `docs/webapp-polling.md`: add the wait protocol (single call, silent reconnect on clean timeout, short-poll fallback), remove the "every 60 s" status instruction, drop the clipboard fallback.
- Update `feature-requirements/SKILL.md` Step 6: replace the 5 s poll loop + 60 s line with the wait call + silent reconnect + short-poll fallback.
- Run the Phase 2 grep verifications; open the Phase 2 MR.

## Phase 1

**What's built:** the `.../synthesis/wait` endpoint, the `SynthesisState`/`_read_synthesis_state` refactor it shares with the existing read, the `config.wait_timeout()` accessor, and a full test module. One MR in feature-skills-webapp; must merge before Phase 2.

**Sequence (test-first):** write `synthesis_wait_test.py` first, then implement `config.wait_timeout()`, the read-helper refactor, the handler, and the route until the suite is green.

**Tests needed** (each must fail without the endpoint):

- Already-submitted doc → immediate 200 with the full responses payload (TestClient).
- Wake-on-submit → a submission during the wait wakes it and returns the responses (direct coroutine: task + `sleep(0)` + POST/insert + broadcast).
- Coarse-signal re-check → a broadcast for a *different* doc does not satisfy the wait; it keeps waiting until this doc submits or times out.
- Timeout → with a tiny `wait_timeout()`, an unsubmitted doc returns 200 `submitted=false`.
- Cleanup → `broadcaster.client_count == 0` after submit, after timeout, and after cancelling the handler task (disconnect).
- Missing broadcaster → immediate `submitted=false` (no crash).
- 404 for unknown logical key; 503 when `db_path is None`.
- Read-error propagation → monkeypatch `_read_synthesis_state` to raise; assert it surfaces (500) and the queue is still unregistered.

## Phase 2

**What's built:** the consuming-side switch, in the feature-skills repo. One MR.

**`docs/webapp-polling.md`:** add a "wait" step to the keyed protocol — issue a single `GET .../synthesis/wait`; on `submitted=true` consume responses; on a clean `submitted=false` timeout, **silently re-issue** the wait (no status line); if the call errors or the connection is refused, fall back to the existing 5 s short poll. Remove the "Periodic status — emit a 'still waiting…' line every 60 s" instruction. Drop the clipboard "Copy responses" fallback (dead path) in favour of the short-poll degradation.

**`feature-requirements/SKILL.md` (Step 6):** replace the "Poll … every 5 seconds" block and its 60 s "still waiting" line with the wait call + silent reconnect + short-poll fallback, pointing at the updated convention doc.

**Verification:** prose/skill changes — verified by grep assertions (see Verification), since there is no automated test harness for skill markdown.
