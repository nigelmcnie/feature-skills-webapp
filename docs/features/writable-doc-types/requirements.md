# writable-doc-types — Requirements

## Summary

The webapp lets an agent draft a document, hand it to you for review, collect your feedback, and track it through the inbox. Today only four kinds of document can be *written* over the API: `context`, `requirements`, `plan`, and feedback docs (`*-feedback`). Anything else is rejected at the write boundary (`validate_writable`) with "doc_type … is not writable".

That gate is nearly the whole of what stands in the way. Rendering, diffing, and markdown export already cope with an arbitrary document type — the renderer falls back to an opaque "whole body" treatment and keeps the document's styling, the diff view works on any document, and the exporter has a generic converter (verified in the sibling `feature-skills` repo). One reviewing affordance does *not* yet reach bespoke docs — inline click-to-comment is offered only for `requirements` and `plan` — so extending it is part of this work.

The concrete itch: a set of "north-star" planning documents (a vision narrative, a system map, contracts, trust-architecture and per-component deep dives) currently masquerade as `requirements` docs, with mislabelled section headings and an apologetic authoring note explaining the borrowed vehicle. More generally: any document-shaped artefact a developer wants to draft, review, and track — a design doc, a decision record, a report — should be able to exist under its own name.

This feature widens the write boundary so bespoke document types can be submitted over the API, extends inline commenting to reach them, and gives unknown types a defined (rather than incidental) home in the inbox and feature pages.

## Scope

In scope: widening the agent-submission write path so bespoke, feature-scoped document types are accepted; extending inline click-to-comment to any non-feedback document so bespoke docs can be reviewed the same way; and giving unknown types a defined badge and position in the inbox and feature-overview listings.

The change is small in spirit — storage, rendering, diffing, and export already handle arbitrary types. This feature removes an artificial write gate and tidies the few places where unknown types currently fall through to incidental behaviour or miss an affordance.

## Vision

An agent can PUT a document of any sensible type — `vision`, `system-map`, `decision-record` — and it renders, reviews, comments, exports, and appears in the inbox as a first-class document, without that type being hard-coded anywhere.

## Non goals

- **No per-type section manifests.** Bespoke types are opaque (authored as a full HTML body) for v1; a registry that lets a bespoke type declare named sections is deferred. A consequence we accept: an opaque doc has a single body-level section, so its inbox change-labels and diffs are whole-body, not per-section — the registry is what would buy that granularity back later.
- **No change to the trust model.** The webapp stays localhost-only with no auth; widening the boundary opens no new network surface.
- **No implicit parent creation.** A write still requires its project and feature to exist first.
- **No new export wiring in this repo.** The generic markdown converter already handles unknown types; whether a given project exports a bespoke type stays a per-project `.feature-workflow.toml` choice.
- **No change to feedback-doc semantics.** The awaiting-input lane, free instance numbers, sibling-nav exclusion, and comment exclusion remain `*-feedback`-only — the feedback synthesis doc is the one document kind that stays non-commentable.

## User stories

1. As a developer, I want to draft a north-star vision document under its own type so that when I PUT `ai-eng-planning/north-star-vision/vision/1` it is accepted, renders with its own styling, and no longer needs an apology note explaining why it is pretending to be a requirements doc.
2. As a developer reviewing a bespoke doc, I want to leave inline comments on it so that I can annotate a `vision` doc passage-by-passage exactly as I do a requirements doc, rather than only being able to feed back through chat.
3. As a developer reviewing in the inbox, I want a bespoke document to appear in the feature's document list so that after an agent writes a `system-map` doc for a feature, I see it on that feature's page rather than it silently vanishing because its type is not in the known set.
4. As an agent authoring a bespoke doc, I want a clear error when I pick a reserved name so that attempting to write a feature-scoped type called `features` or `review` — names that already carry tracker/UI meaning — is rejected with a message. (Feedback docs, `*-feedback`, remain writable as before; they are not a bespoke type.)
5. As a developer opening a bespoke doc, I want the sibling-nav and badge to behave so that a `decision-record` doc shows a sensible badge and sits in a defined position among its siblings, not an unstyled default that looks broken.

## Data model

No schema change. The `documents.type` column is already free-text, and the versioned content store already persists an opaque whole-body shape — the same shape feedback docs use. Bespoke types reuse both as-is.

The one "model" concern is the **type-name namespace**. Two names are reserved and must stay reserved: `features` (the project-level tracker) and `review` (it holds a sort-rank and label in the inbox/feature UI even though it has no manifest). The `*-feedback` suffix is also reserved — but as an *admitted* type with its own semantics, not a bespoke type and not a rejected name. Bespoke types occupy the remaining, feature-scoped name space.

## Technical approach

### Widen the write boundary

`validate_writable` is the single write-time gate. Today it admits a type only if it is one of the three section types or ends with `-feedback`. The change: admit any feature-scoped type that is *not* a reserved name, while preserving the existing invariants — feature must be present (no project-level bespoke docs), instance must be 1 (free instances stay a feedback-only affordance), and the 1 MB body cap and full-replacement semantics are unchanged. Because the manifest layer already treats unrecognised types as opaque, no new storage, render, or diff path is needed — a bespoke doc is authored as a full HTML body, like a feedback doc. A fat-fingered type (e.g. `requirement`) would otherwise create a silent orphan doc, so a write that introduces a previously-unseen type emits a non-blocking signal for discoverability.

### Extend inline commenting

Inline click-to-comment is currently offered only for a fixed pair of types. The rule we want is simpler: any active, feature-scoped document that is *not* a feedback synthesis doc is commentable. Widening it lets bespoke docs — and context docs — be reviewed passage-by-passage, not only via chat. The comment-submission endpoint already accepts any document, so this is a matter of the render layer offering the affordance on the opaque doc path.

### Give unknown types a defined home

Two read-model surfaces treat unknown types as an afterthought: the feature-overview page drops them from its document list entirely, and the inbox badge derives an undefined style class from the raw type string. Unknown types need inclusion in the overview list (ranked after the known types) and a defined default badge. Sibling-nav already includes and ranks them, so it needs confirmation, not surgery.

## Testing

- Boundary tests on `validate_writable`: a bespoke feature-scoped type at instance 1 is admitted; the reserved names `features` and `review`, a project-level (no feature) write, and instance ≠ 1 are each rejected with a clear message; a `-feedback` name is still admitted. Each reject case must be shown to fail *without* the widening (red on the parent commit) so it pins the boundary, not the harness.
- A write→read round-trip for a bespoke type asserts the stored opaque body round-trips unchanged.
- Commenting: a bespoke doc (and a context doc) is commentable; a feedback synthesis doc is not — asserting the observable rendered affordance, not internal calls.
- Presentation: a bespoke doc appears in the feature-overview list and carries the defined badge kind.

## Alternatives

1. Explicit allowlist (per-project or global registry of permitted types) Context doc — open questions; round 1 review Rejected for v1 — but not purely on security. On a single-user localhost tool the allowlist's real value is not gating an untrusted writer; it is catching typos (a mistyped `requirement` would be refused) and doubling as a discoverability list of known types. The reserved-name denylist plus a non-blocking new-type signal recovers most of that benefit without the friction of a curated list that drifts stale — so the denylist wins.
2. Section-manifest registry for bespoke types Context doc — open questions Deferred, not rejected. Letting a bespoke type declare named sections would gain auto-TOC and, notably, per-section diffs and inbox change-labels that opaque docs give up — but it is a larger build. Opaque bodies already render, comment, and export; v1 ships the boundary and revisits structured sections if the need proves out.
3. Free instance numbers for bespoke types Context doc — open questions Rejected for v1. Free instances exist so feedback can iterate in numbered rounds; a bespoke document is a single canonical artefact. Pinning instance to 1 keeps its logical key stable and predictable. If rounds are wanted, that is what feedback docs are for.

## Delivery phases

### Phase 1 — Widen the write boundary

Change `validate_writable` to admit bespoke feature-scoped types, rejecting the reserved names (`features`, `review`) while continuing to admit `-feedback`, and preserving the feature-required, instance-1, size-cap, and full-replacement invariants. A write that introduces a previously-unseen type emits a non-blocking discoverability signal. Cover admit and reject cases with tests. On its own this makes a bespoke doc writable and — via the existing opaque path — immediately renderable at `/doc/N` and diffable. One MR.

### Phase 2 — First-class in the reviewing UI

Give unknown types a defined default badge kind, and include bespoke (non-feedback) types in the feature-overview document list rather than dropping them; confirm sibling-nav ordering. Extend inline click-to-comment to any non-feedback document so bespoke docs (and context docs) can be reviewed passage-by-passage. This makes the Vision true: a bespoke doc renders, reviews, comments, and appears first-class in the inbox and feature pages. One MR.

### Phase 3 — Migrate the motivating documents

In the `ai-eng-planning` project, rewrite the two north-star documents to their natural types (`vision`, `system-map`) and drop the "riding as requirements" vehicle notes. This is content migration over the API — no code change in this repo — and validates the feature end-to-end on the case that motivated it. Gated on Phases 1–2.

## Indicative notes

- The gate lives in `feature_skills_webapp/storage/documents.py` (`validate_writable`, `WRITABLE_SECTION_TYPES`); the reserved suffix constant is `FEEDBACK_SUFFIX` in `storage/walker.py`.
- Commenting: the `is_commentable` predicate in `web/doc_view.py` (today `{requirements, plan}` + native render mode, ~line 97) and the `{% if is_commentable %}` block in `templates/doc.html`; the opaque render path needs the comment layer wired in (currently only "native" mode emits it). The POST endpoint (`web/comments.py`) already accepts any doc-id.
- Presentation: `badge_kind` in `storage/inbox.py`; feature-page filter in `web/feature_page.py` (the `type in DOC_TYPE_ORDER` list comprehension); sibling-nav in `web/doc_view.py` `siblings()`.
- New-type signal: likely a distinct event row when a write creates a previously-unseen type; keep it non-blocking (no effect on the write result).
- Default badge: either a new stylesheet badge class for a generic "doc" kind or mapping unknown kinds onto an existing neutral badge — pick one and ground any new class in `doc.css`.
- Opaque authoring: bespoke docs supply a full contract-grounded `body` and cannot use `sections`/`extra_css` — no auto-TOC, whole-body diffs.

## Design notes

- **Type admission:** any-type-goes with a reserved-name denylist, not an allowlist — there is no untrusted writer on a localhost tool; typo/discoverability is handled by a non-blocking new-type signal instead (round 1).
- **Reserved names:** `features` and `review` (both already carry tracker/UI meaning); the `-feedback` suffix stays an admitted type with its own semantics, not a rejected name (round 1).
- **Commenting rule:** any active, feature-scoped document that is not a feedback synthesis doc is commentable — deliberately generalised beyond bespoke types to also cover context docs (round 1).
- **Shape:** opaque-only for v1; the section-manifest registry is deferred. Accepted cost: whole-body (not per-section) diffs and inbox change-labels for bespoke docs (round 1).
- **Instance:** pinned at 1 for bespoke types; free instances remain a feedback-only affordance (round 1).
