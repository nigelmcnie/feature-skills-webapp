# agent-submission-mcp-facade — Context

## Problem space

The agent-submission arc (`agent-submission-api`, `agent-submission-tracker-ops`) deliberately shipped **HTTP-first**: a complete set of logical-key operations for agents to create/update context/requirements/plan/feedback docs, read comments/synthesis, and manage the feature tracker — all over `http://127.0.0.1:8800`. The deferred-but-intended next step, named in three shipped features but never captured as its own feature, is a **typed MCP surface** over those same operations.

The motivation is cross-agent reach. Today the feature workflow assumes Claude Code and curls the HTTP API from skills. An MCP facade would let any MCP-capable agent (notably Codex, the stated parallel goal) mount the same operation set as typed tools, without each agent hand-rolling HTTP calls and path/key handling. The decision recorded in `agent-submission-api` is explicit: *"HTTP is the v1 contract. An MCP facade is a thin shim over the same operations and is added later — layer them, don't choose. One set of operations, not two implementations."*

## Related work

This sits directly on top of the shipped agent-submission substrate:

- **`agent-submission-api`** — the logical-key HTTP contract (`PUT/GET /api/documents/{project}/{feature}/{doc_type}/{instance}`, manifests, comments/synthesis reads) in `web/submit.py` and `storage/documents.py`. Its context already frames the MCP shim as in-scope-later and flags the in-process-vs-separate-server question.
- **`agent-submission-tracker-ops`** — the six tracker endpoints in `web/tracker.py` (listing + capture/claim/ship mutations). The MCP surface should cover these too, so an agent can run the whole workflow over MCP.
- **`versioned-content-store`** — laid the load-bearing groundwork: decoupling document identity from `source_path` onto a stable `logical_key` (migration 0003), so a non-file writer (HTTP or MCP) converges on the same row. The substrate for any non-file writer already exists; only the MCP facade itself is unbuilt.

The webapp is a Starlette HTTP app, so the natural shape is an MCP server that calls the same `storage/` operations the HTTP handlers already call — sharing the operation layer, not duplicating it.

## Constraints

- **One operation set, two surfaces.** The MCP tools must be a thin typed shim over the existing operations — not a reimplementation. Refactor toward a shared operation layer if the HTTP handlers don't already expose one cleanly.
- **Trust model is unchanged.** The HTTP API is localhost / single-user / no-auth. An MCP surface inherits the same boundary; this is not the feature that adds auth.
- **Doesn't replace HTTP.** HTTP stays the v1 contract and the skills' current consumer. MCP is additive — existing curl-based skills keep working.
- **Cross-agent intent.** The point is usability from Codex as well as Claude, so the tool definitions should be agent-neutral and typed against the manifests rather than assuming Claude-specific behaviour.

## Links

- Builds on: `agent-submission-api`, `agent-submission-tracker-ops`, `versioned-content-store` context/requirements docs.
- Cross-agent goal: the Codex-compatibility plan referenced in `versioned-content-store`'s context.

## Open questions

- **In-process vs separate server.** Is the MCP shim mounted in-process inside the Starlette webapp (sharing its event loop and storage layer directly), or a separate MCP server process that talks to the webapp over HTTP? `agent-submission-api` flagged this as the key topology question.
- **Operation coverage for v1.** Document submit/read only, or the full tracker mutation set (capture/claim/ship) and comments/synthesis reads from the outset?
- **Transport.** stdio (per-agent spawn, matching how Claude Code mounts MCP servers) vs a long-lived HTTP/SSE MCP transport against the already-running webapp service.
- **Manifest-driven typing.** Should the tool schemas be generated from the existing doc-type manifests so the MCP surface stays in lockstep with the section contracts, rather than hand-maintained?
- **Does this subsume the skills cutover?** Or is switching the feature-skills skills from curl to MCP a separate downstream feature once the surface exists?
