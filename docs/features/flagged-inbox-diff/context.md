# flagged-inbox-diff

## Problem space and motivation

This is the feature the whole structured-content arc was built for. The original pain: the inbox's "New since last visit" is genuinely useful when it lists docs that need review as part of a workflow step (a requirements doc created, a review doc created). But during *implementation* and *review*, those docs get re-touched, re-surface in the list, and you click in only to think *"haven't we done this already?"* — no sign of what changed, and a full re-read to find out. The wish wasn't to drop them from the list, but to keep them there **flagged with the reason**, with a **diff view** available, easy to toggle against the raw doc, and defaulting to the diff when the reason was itself a change.

Crucially, the shape of this problem has changed since it was first raised, because F1 and F2 shipped in between. The original pain had two halves: (a) docs re-bubbling for *trivial / no-op* reasons, and (b) when they do, no reason and no delta. **Half (a) is already solved.** F1's walker now cuts a version and emits an `updated` event *only when content actually changed* (`walker.py`: `serialise(cur) != serialise(content)`); an mtime-only touch or a whitespace re-save that serialises identically produces no event, no version, and no re-bubble. So the empty re-surfacing that caused most of the irritation is gone at the root.

What remains is the *legitimate* re-bubble: content genuinely changed, or comment/synthesis activity happened — and you want to know which, and read only the delta. That is now a clean, well-scoped feature, and it needs **only F1 and F2**, both shipped.

## Related work

- **[versioned-content-store](../versioned-content-store/context.html) (F1, shipped).** The enabler. Gave us the `document_versions` table (`version_num`, `content_json` = a serialised `{shape, sections:[{key, body}]}`, `actor`, `created_at`) and the content-changed-only event/version gating that already killed no-op re-bubbles. Because content is section-structured, **section-level diffs fall straight out**. `storage/versions.py` exposes `current_content()`; versions are ordered by `version_num`.
- **[server-rendered-docs](../server-rendered-docs/context.html) (F2, shipped).** Removed the iframe — docs render natively (`web/doc_render.py`, `web/doc_view.py`). So a diff view is just **another server-rendered mode of the same page**, and the raw↔diff toggle is trivial UI rather than the iframe-swap hack the original brainstorm assumed.
- **The inbox read model.** `storage/inbox.py` — `new_since_last_visit` bubbles any active doc with an event newer than `read_state.last_read_at`; `InboxCard` currently carries `project / feature / label / last_activity / document_id / badge` and **no reason**. The "what I last saw" anchor (`last_read_at`) is exactly what pins the diff's baseline.
- **[agent-submission-api](../agent-submission-api/context.html) (later, not a dependency).** Where *meaningful* `actor` values will come from — see the limitation below. This feature is independent of it.

## Constraints and considerations

**Three pieces.** (1) A **diff**: pair is `prior` = the latest version with `created_at ≤ last_read_at` against `current` = the latest version; section-aware (match on section `key`: added / removed / changed / unchanged) with an intra-section text diff for changed bodies. A first sighting (no prior version) has nothing to diff — show raw. (2) A **reason flag** on the inbox card — `InboxCard` gains a reason, e.g. "Updated — N sections changed", "comments added", or "new". (3) **Default to diff**: the card deep-links to the diff view when the re-bubble is a content change with a prior version; otherwise raw, with a toggle either way.

**Reason scope: content *and* interaction (decided).** A doc re-surfaces two ways: a new content version, or comment/synthesis activity (`comment_submitted` / `comment_integrated` events, synthesis submissions) with *no* content version. Both get flagged. Only the content case has a diff; the interaction case flags "comments added" and falls back to the raw view.

**The honest limitation: the semantic "why" isn't available yet.** The reason originally wished for — "applying feedback" vs "an implementing agent edited it" vs "you revised it yourself" — needs `actor` to be meaningful, but `actor` is uniformly `"importer"` today, because the file-walker is the only writer (all three `record_version` calls pass `actor="importer"`). Rich actors arrive with [agent-submission-api](../agent-submission-api/context.html). So this feature should derive the reason **structurally** (which sections moved, content vs comments vs new) and treat actor-based semantic reasons as a **forward-compatible enrichment** that lights up later — *not* block on it. The seam already exists: `document_versions` stores `actor` per version.

**Opaque-body doc types.** Feedback docs and the features tracker ride as opaque whole-bodies (per F1), not section-structured — so their diff is a single-body text diff, not a per-section one. Worth handling explicitly rather than assuming every doc has sections.

**Dependencies.** F1 + F2 only, both shipped. Independent of agent-submission-api.

## Links

- Enabler F1: [versioned-content-store](../versioned-content-store/context.html) (versions, content-change gating).
- Enabler F2: [server-rendered-docs](../server-rendered-docs/context.html) (native render = trivial diff mode).
- Inbox read model: `feature_skills_webapp/storage/inbox.py` (InboxCard + new_since_last_visit).
- Version model: `feature_skills_webapp/storage/versions.py`, `storage/doc_content.py` (ParsedContent / Section / serialise).
- Render seam: `feature_skills_webapp/web/doc_render.py`, `web/doc_view.py`.
- Forward enrichment: [agent-submission-api](../agent-submission-api/context.html) (meaningful actors).

## Open questions

1. **Intra-section diff granularity.** Diff the *rendered text* of a section (more readable, and `doc_render` already knows how to render a section) or the *raw HTML fragment*? Leaning rendered-text. Bodies are opaque trusted HTML, so a raw-fragment diff would surface markup noise.
2. **Mark-read on the diff view.** Viewing the diff should stamp `read_state` (advancing the "last seen version") so the doc stops flagging next time — confirm, and confirm a timestamp anchor is enough given versions carry `created_at` (no need to store a per-document "last seen version_num").
3. **How the card communicates "no diff, just comments".** Interaction re-bubbles have no content version, so the reason taxonomy spans content-diff vs interaction — the card and the doc view need to make the absence of a diff legible rather than landing on an empty diff.
4. **Multiple versions since last read.** If several versions accrued, the diff should collapse to last-seen → current (which the `created_at ≤ last_read_at` anchor gives directly). Confirm we don't want an inter-version stepper in v1.
5. **Where the toggle lives and how the card deep-links.** Sub-route (`/doc/{id}/diff`) vs query param (`?view=diff`), and how the inbox card links straight to the defaulted-diff view.
6. **Naming sections in the flag.** Section `key`s are manifest keys (e.g. `technical-approach`); humanise them for the card label ("Technical approach changed") rather than showing raw keys.
