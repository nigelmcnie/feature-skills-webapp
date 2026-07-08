# document-archive-api — Context

## Problem space

The `writable-doc-types` feature opened the API to *writing* bespoke document types, but nothing lets you *retire* a superseded API-authored document. A document's `archived` status is only ever set by the walker/importer, and only when a source *file* disappears (`storage/documents.py`, the importer event routing). API-authored docs have no source file, so no path — walker or API — ever archives them. The feature `drop` endpoint archives a whole *feature*, not an individual doc, so it can't retire just the old copy when one doc supersedes another.

This bit us directly in the `writable-doc-types` Phase 3 migration: moving the ai-eng-planning north-star docs onto their natural `vision` / `system-map` types left the old `requirements`-typed originals stranded in each feature's active Documents list with now-stale content, and the only way to retire them was a direct `UPDATE documents SET status='archived'` against the deployed SQLite DB — outside the API entirely.

## Related work

- **writable-doc-types** — the just-shipped feature that opened bespoke doc-type writes; this is its natural follow-up (write was addressed, retire was not).
- **Walker archival semantics** — `submit_document`'s importer branch sets `archived`/`missing`/`reactivated` based on source-file state; the API write branch never sets status.
- **Feature `drop` endpoint** (`/api/projects/{p}/features/{f}/drop`) — feature-level archival; the closest existing lever, but too coarse for a single doc.
- **Read models that already honour status** — `feature_page.py` splits active vs archived; `doc_view.py` renders an archived doc with a flag. So an archived status is already respected end-to-end; only the *setter* is missing for API docs.

## Constraints

- Preserve versioned history — archiving retires a doc from the active list, it does not delete its versions.
- Localhost / no-auth trust model unchanged.
- The walker's file-sourced archival must not conflict with an API-set archived status (API docs have no source path, so a re-walk won't touch them — but the interaction should be reasoned through, not assumed).
- Reversibility is desirable: an unarchive path so a retirement can be undone without a DB edit.

## Links

- Triggering work: the `writable-doc-types` context / requirements / plan docs in this project.
- The migration that exposed the gap: ai-eng-planning `north-star-vision` / `north-star-system-map` (now on their natural types; originals archived via direct DB write).

## Open questions

- Endpoint shape: a dedicated `POST /doc/{id}/archive` (+ an unarchive), or a status field on the logical-key `PUT`? The former keeps writes as full-replacement content; the latter overloads the write path.
- Should it support hard delete, or archive-only? Archive-only preserves history and matches the existing status vocabulary.
- Should archived API docs be excluded from any surfaces beyond the feature page (e.g. sibling-nav, inbox) — and does that differ from how file-sourced archived docs behave today?
- Does retiring the *current* instance of a logical key need any guard (e.g. you can still GET it, but it drops off active lists)?
