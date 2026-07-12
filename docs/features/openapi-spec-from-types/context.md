# openapi-spec-from-types — Context

## Problem space

The OpenAPI spec (`web/openapi.py`) is hand-authored as literal dicts (`API_METADATA`), structurally disconnected from the handlers it documents. Because nothing ties the two together, the spec *drifts*. This session an agent hit a `400` because the document-write `sections` body was declared as an `array` while `build_content` requires an object map (`{"key": "html"}`) — the spec described a shape the server rejects outright. It was patched (`ca93671`), but the *class* of defect stands, and the just-shipped `document-archive-api` / `feature-archive-semantics` work added *more* hand-authored schema (the archive endpoints' `reason` enums, `superseded_by`, etc.), widening the surface that can rot.

The tests only check the spec is well-formed and that every route appears — not that any request schema matches what its handler actually accepts. The proper fix: derive request/response schemas from the endpoints' own typing information, so the spec and the handlers share **one source of truth** and drift is impossible *by construction* rather than caught after the fact.

## Related work

- **The spec-fix (`ca93671`)** — patched one drift instance (`sections` array→object); did nothing about the class.
- **`web/openapi_test.py`** — asserts coverage (`:30`), route parity (`:41`), no phantom ops (`:49`), structural validity (`:58`, `validate(spec)` — well-formed, not *accurate*), and requestBody *presence* (`:154`). It does **not** assert schema conformance to handler behaviour — that gap is why the drift survived.
- **The typed shapes that should drive generation** — `build_content` (`sections: dict[str, str]`, `storage/documents.py`), the `submit` handlers' raw `body.get(...)` parsing, the archive endpoints, and the `ARCHIVE_REASONS` / `DOC_ARCHIVE_REASONS` enums (already *imported* into `openapi.py` — a partial, manual coupling that hints at the direction to take further).
- **The stack** — Starlette + a hand-written `API_METADATA` dict; stdlib-leaning (`uv`, no pydantic today).

## Constraints

- **Light-dependency ethos.** The project is Starlette + SQLite and parses raw dict bodies. A derivation approach should prefer typed request models (`TypedDict` / dataclasses) + a small JSON-Schema deriver over a heavy framework — or explicitly justify a dependency (`msgspec` / `pydantic`) if it earns its place.
- **Represent the real API faithfully**, including the read/write asymmetry (`sections` is read as `[{key, body}]` but written as `{key: body}`). Generation must not silently “symmetrise” it — only a deliberate handler change should.
- **Preserve the curated niceties** the hand spec carries and the tests assert — per-operation summaries, examples, error responses, query-param docs. Likely a hybrid: derive *schemas* from types, keep curated prose/examples as annotations layered on top.
- Localhost / no-auth model unchanged.

## Links

- The drift + fix: `web/openapi.py` (`API_METADATA`), commit `ca93671`.
- The conformance gap: `web/openapi_test.py`.
- Typed shapes to derive from: `storage/documents.py` (`build_content`), `web/submit.py`, the archive endpoints, `ARCHIVE_REASONS` / `DOC_ARCHIVE_REASONS`.
- Motivating incident: the session retro that surfaced the `sections` 400.

## Open questions

- **Mechanism** — introduce typed request models (`TypedDict` / dataclass) at each write boundary and derive JSON Schema from them? Adopt a validation lib that emits JSON Schema (`msgspec`, `pydantic`)? Or annotate handlers and introspect their signatures?
- **Collapse validation too?** Today `build_content` hand-validates (`'sections' must be an object`). Should the derived model also *do* the parse/validation, making one artefact both validate requests and generate the schema?
- **Where do request models live** — beside their handlers, or a dedicated `schemas` module?
- **Bridge test** — until full derivation lands, add a conformance test that feeds each documented `requestBody` example through the real handler (accepts a valid payload, rejects a malformed one), so hand-authored schemas can't drift silently in the meantime.
- **Scope** — all routes at once, or start with document-write + the archive endpoints (the ones that bit us) and expand?
- **Fix the asymmetry here?** Take the opportunity to also accept the array shape on write (making read/write symmetric), or keep the asymmetry and just document it faithfully?
