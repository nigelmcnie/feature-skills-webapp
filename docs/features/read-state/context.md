# read-state

## Problem space and motivation

`doc-discovery` shipped the *spine* of the inbox: a fresh `documents` index and an `events` row per discovered change. But the inbox's headline question — *"what's new since I last looked?"* — needs the other half of the comparison. An event is only "new" relative to *when I last read that doc*, and nothing records that yet. The `read_state(document_id, last_read_at)` table was laid down empty in `webapp-skeleton`'s `0001_init.sql` and has had no writer since.

`read-state` is that writer. It's the third feature in the build order and the last piece of plumbing before any user-facing view: `inbox-view` derives its "New since last visit" category from `documents with events newer than read_state.last_read_at`, and `doc-view` stamps `last_read_at` on render. Both list `read-state` as a dependency. The feature is small by design — a single table's worth of writes plus an "is this unread?" helper — but everything downstream is read-state-shaped, so getting the read model right here is what makes the inbox cheap and correct later.

## Related work

- **`doc-discovery` (shipped).** Emits the `events` rows (`created` / `updated` / `archived` / `missing` / `reactivated`, each with a populated `payload_json`) that `read-state` compares against. Crucially, it *marks* vanished docs `missing` rather than deleting them — a decision made *specifically* to protect the `read_state` / `comments` / `synthesis` rows that cascade off `documents` and that this feature is the first to populate. See its [context](../doc-discovery/context.html), [requirements](../doc-discovery/requirements.html) and [plan](../doc-discovery/plan.html).
- **`webapp-skeleton` (shipped).** Laid the `read_state` table in `0001_init.sql` and ported kea's `connect()` / `transaction()` / per-request-connection pattern. read-state's upserts should go through the same `transaction()` helper (autocommit + explicit `BEGIN IMMEDIATE`), not bare `with conn:`. See its [context](../webapp-skeleton/context.html).
- **The `events` table is the audit log the inbox derives from.** The design doc frames the inbox as "docs with events newer than `last_read_at`, grouped by type". So the "is this doc unread?" helper read-state ships — `max(events.created_at for doc) > last_read_at` — is not a convenience; it's the exact predicate `inbox-view` will call.
- **Downstream: `inbox-view` and `doc-view`.** Both depend on this feature. `doc-view` is where the per-render stamp naturally lives ("Stamp `read_state.last_read_at` on render"), which raises a sequencing question (below), since `doc-view` lands *after* read-state in the build order.

## Constraints and considerations

- **Single user — no user table.** The design doc is explicit: hardcode read-state to a single row keyed by document. No per-user dimension, now or planned.
- **Stamp on render, not on close.** A deliberate design call: "closer-to-correct is the enemy of perfect-and-complex." Opening a doc marks it read, full stop — no scroll tracking, no dwell-time, no close-beacon.
- **Lazy upsert, one row per document.** The scope is `upsert read_state(document_id, last_read_at=now())` on view. A never-viewed doc has no row at all — consumers must treat a missing row as "fully unread" (every event is new), not as an error.
- **Timestamps must be comparable.** The whole feature is a `>` comparison between `last_read_at` and `events.created_at`. Both must share the storage format and timezone `doc-discovery` already used for `events.created_at` (e.g. UTC). A format mismatch silently breaks the unread predicate.
- **No migration likely needed.** The `read_state` table already exists from `0001_init.sql`. Unless an index is wanted for the unread helper, this feature may be pure write-logic + helper + admin action, with no schema change.
- **Bulk "mark all read".** The design names one admin action: "mark all read for this project". Needs a decision on which docs it touches (active only? archived/missing too?) and whether it upserts a row per document or is computed.
- **No-network test discipline.** Inherits the skeleton's harness: `pytest-socket` with sockets disabled, xdist with a per-worker DB. Exercise read-state against a temp DB — seed documents + events, stamp, assert the unread predicate flips.

## Links

- Design doc: [feature-skills webapp design](file:///home/nigel/src/nigelmcnie/feature-skills/docs/webapp.html) — §6 `read-state` feature card, §4 data model (`read_state(document_id, last_read_at)`), §6 inbox derivation.
- Depends on: [doc-discovery](../doc-discovery/context.html) (shipped — `events` spine, `documents` index) and [webapp-skeleton](../webapp-skeleton/context.html) (shipped — `read_state` table, `transaction()` helper).

## Open questions

1. **Where does the stamp actually fire?** The design card says "stamp on every doc-view request", but `doc-view` is a later feature. Does `read-state` ship the `read_state` write path + the unread helper + the bulk admin action now, and leave the per-render stamp call to be wired in when `doc-view` lands? Or does it expose a minimal stamp endpoint so the write path is exercised end-to-end before `doc-view` exists?
2. **How is the unread predicate kept cheap?** `max(events.created_at) > last_read_at` over every document is the inbox's hot path. Does this feature add an index on `events(document_id, created_at)`, or do `doc-discovery`'s FK indexes already suffice?
3. **What's "unread" for a doc with no read_state row?** Lazy upsert means most docs start with no row. Confirm the contract: no row ⇒ `last_read_at = -∞` ⇒ every event counts as new. This is what makes a freshly indexed doc show up in the inbox at all.
4. **What does "mark all read for this project" cover?** Active docs only, or archived/missing too? Does it write a `last_read_at = now()` row per document, and should it emit anything (it's a read action, so presumably no `events` row)?
5. **Do archived / missing docs participate in unread at all?** A `missing` doc keeps its `read_state` row by design — but should it ever surface as "new since last visit", or are non-`active` docs filtered out of the unread calculation entirely?
