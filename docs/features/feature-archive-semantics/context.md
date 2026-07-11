# feature-archive-semantics — Context

## Problem space

Feature-level archival already exists, but it is *bare*. The `drop` verb (`POST /api/projects/{p}/features/{f}/drop` → `drop_feature`, `storage/tracker.py:308`) sets `features.status='archived'` with no reason, no pointer to where the work went, and no way back (nothing transitions out of `archived`; `claim` only comes from `available`/`parked`). So a dropped feature just silently vanishes from Available with no record of *why* it is gone or *where it went*.

Two real retirement reasons motivate a richer verb, and the distinction is the whole point — it makes the tracker self-explaining:

- **subsumed** — the outcome shipped, but as part of *another* feature, not as its own line of work (e.g. `semantic-carveout-llm-exemption-realiser`, delivered as Phase 3 of another feature). Distinct from **Done**, which implies it was worked as its own line.
- **superseded** — a design decision dissolved the need before it was built; the premise no longer holds (e.g. `segment-carveout-routing-rationale` — a redesign stopped segmentation routing carve-outs at all, so there is no rationale left to capture). Distinct from **Parked**, which might come back; this will not.

This is the sibling of `document-archive-api` at the *feature* layer: same “points somewhere” design language, different object (tracker rows, not documents).

## Related work

- **The existing `drop` verb** (`drop_feature`, `storage/tracker.py:308`) — the thing to enrich or replace. `archived` is *already* a feature status (`FEATURE_STATUSES`, `tracker.py:11`), so this is not a new status; what is missing is the metadata, the reversibility, and the rendering.
- **Sibling: `document-archive-api`** — the same archive design at the document layer. It carries an optional `reason` + `superseded_by` + `note`; this feature should share that *shape* (the feature enum is the fuller set — `subsumed` / `superseded` / `duplicate` / `obsolete`; a document is rarely “subsumed”).
- **Feature-verb precedent** (`claim` / `park` / `release` / `ship` / `drop`) — `POST` to a verb subpath, idempotent, returning a `MutationResult` with a `changed` flag. The new verb should mirror this.
- **Read surfaces already exclude archived features** — the inbox filters `f.status NOT IN ('parked','done','archived')` (`inbox.py`), and the tracker Available list only shows `available`. So an archived feature already drops off feature-choice / recommendations; only a distinct *rendered* Archived section is new.

## Constraints

- **Non-destructive.** The feature stays GETtable and its context / requirements / plan docs persist (audit trail); archiving only removes it from Available so feature-choice and recommendations stop surfacing it.
- **New columns → a migration.** The `features` table is `status / owner / notes / timestamps` only (`migrations/0001_init.sql:8`). `reason`, `superseded_by`, `note`, `archived_at` are all new.
- **Reversibility.** An `unarchive` (or re-`claim`) returns the feature to `available`, for the “we were wrong, it is live again” case. This is new work — `drop` is one-way today.
- **One archival path.** Do not ship a second verb that also sets `status='archived'` with divergent semantics. Either enrich `drop`, or add `archive` and retire `drop` — decide, don't fork.
- Localhost / no-auth trust model unchanged.

## Links

- Sibling: `feature-skills-webapp/document-archive-api` (shares the `reason` / `superseded_by` / `note` vocabulary).
- Code: `drop_feature` (`storage/tracker.py:308`), `FEATURE_STATUSES` (`tracker.py:11`), `features` DDL (`migrations/0001_init.sql:8`), tracker rendering (`web/tracker.py`).
- Motivating features (in their home project): `semantic-carveout-llm-exemption-realiser` (subsumed), `segment-carveout-routing-rationale` (superseded).

## Open questions

Endpoint sketch (from the originating design — mirrors `claim`):

- `POST /api/projects/{project}/features/{feature}/archive` with `{ reason, note, superseded_by, actor }` → `{ status: 'archived', reason, superseded_by, archived_at, note }`.

**To decide in requirements:**

- **Enrich `drop` or add `archive` and retire `drop`?** The one-archival-path constraint forces a choice; adding `archive` as the semantic front door and folding `drop` into it (or aliasing) is the likely shape.
- **`reason` enum** — `subsumed` / `superseded` / `duplicate` / `obsolete`. Which require `superseded_by`? The “no orphan archives” guardrail says at least `subsumed` / `superseded` (and arguably `duplicate`) must point somewhere; `obsolete` may stand alone.
- **`superseded_by` validation** — validate it resolves to a real feature, but accept a free-text MR / decision ref as a fallback (a superseder is not always another tracked feature).
- **Idempotency** — re-archiving an already-archived feature: a no-op (`changed=false`, matching `drop_feature` today) or a `409` carrying the existing archive record? Consistency with the other verbs argues for the no-op.
- **`404` on a missing feature** — archive is deliberate, so fail loudly rather than skip-silently as `claim` does. Confirm.
- **Rendering** — a distinct `## Archived` section in the tracker (`features.md`) with *Feature / Reason / Superseded by / Note* columns, so “why it is gone + where it went” is legible at a glance without cluttering Available.
- **Vocabulary alignment** — keep the `reason` / `superseded_by` / `note` shape identical to `document-archive-api`; only the enum differs (fuller here).
