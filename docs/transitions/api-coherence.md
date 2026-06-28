# api-coherence transition

This document describes the contract changes being made as part of the
api-coherence feature. Changes are being shipped in eight phases, each as a
separate MR. The trust model is unchanged: localhost, single-user, no auth.

## What is changing

Projects and features are becoming **explicit resources** that must be created
before any documents can be written to them. Previously, document writes would
implicitly create any missing project or feature parent; after this transition,
they will return a 404 with a self-explaining error body if the parent does not
exist.

New endpoints:

- `POST /api/projects/{project}` — strict project create (409 if it exists)
- `GET /api/projects/{project}` — single-project read
- `POST /api/projects/{project}/features/{feature}` — strict feature create with
  notes (409 if it exists), replacing the `/capture` verb
- `GET /api/projects/{project}/features/{feature}` — single-feature read

Retired endpoints:

- `POST /api/projects/{project}/features/{feature}/capture` — removed once
  `feature-context` migrates to the new create verb

## How to adapt

If you are writing a document and receive a 404 error, create the feature (and
project, if necessary) first, then retry the document write.

The error body will tell you exactly which resource to create and the endpoint
to use:

```
{"error": "feature 'my-feat' does not exist in project 'my-proj'. Create it first: POST /api/projects/my-proj/features/my-feat"}
```

Skills that call the API directly (`feature-context`) will be updated as part
of this transition. Mid-flight agents that encounter a 404 can recover by
following the instructions in the error body.

## doc-id as canonical write key for comments and synthesis

Comments and synthesis responses are written using the numeric document ID
(`/doc/{document_id}/comments`, `/doc/{document_id:int}/synthesis-response`),
not the logical path. This is intentional: these endpoints are only ever called
by the webapp UI, which already holds the `document_id` from the page it
renders. Logical-path equivalents (e.g. `/api/documents/{project}/{feature}/…/comments`)
were considered and deliberately not added — the extra routing complexity buys
nothing for a single-caller path.

If you need the `document_id` for a given logical key, use the document listing:
`GET /api/projects/{project}/features/{feature}/documents` returns `document_id`
alongside each entry.

## Phase progress

- Phase 0 — notices added to manifest + listing responses; message helpers in place
- Phase 1 — `POST /api/projects/{p}` strict create; `GET /api/projects/{p}` single-project read
- Phase 2 — `POST /api/projects/{p}/features/{f}` strict create (replaces `/capture`); `GET` single-feature read
- Phase 3 — walker import and `feature-context` skill migrated to explicit create-first flow
- Phase 4 — import cycle broken via `storage/parents.py`; walker delegates to `submit_document`
- Phase 5 — `projects.suggested_order` column; `PUT .../suggested-order`; `created_at` on features listing; render-from-DB export in `feature-html-to-md`
- Phase 6 — `GET /api/projects/{p}/features?q=…&status=…` filtering (slug/notes LIKE + status exact-match)
