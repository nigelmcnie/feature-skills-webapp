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

## Phase progress

- Phase 0 (signpost) — notices added to manifest + listing responses; message helpers in place
- Phases 1–7 — forthcoming (see the plan)
