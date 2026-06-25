# decouple-diff-baseline — Context

## Problem space

The section-aware diff (`?view=diff`) is meant to show "what changed since you last read this document". In practice the act of opening a document destroys that diff. Concrete case (doc 307): an agent updated it after a feedback round (v1 18:36 → v2 19:02), told Nigel to look; Nigel opened it and it neither opened in diff mode nor, when toggled to *View changes*, showed anything — just *"No text changes since you last read this document."*

Mechanism: the diff baseline is `read_state.last_read_at`, a single timestamp that `mark_read` bumps to `now()` on **every** view (`doc_view.py:174`). The diff compares current content against `content_at_or_before(last_read_at)`. Opening the doc in plain native mode stamps `last_read_at = 19:08`, which is *after* v2 (19:02), so the baseline version *is* the current version and the diff is empty. The same bump also drops the doc out of the inbox (`new_since_last_visit` needs an event newer than `last_read_at`). The diff is a one-shot that the most natural action — looking at the doc — consumes.

Two coupled faults made it bite here: (1) `last_read_at` conflates "I've seen the document exists" with "I've seen what changed"; (2) the agent-facing API hands back a diff-blind URL, so even the *first* view lands in native mode rather than diff mode.

## Related work

Directly extends **flagged-inbox-diff** (shipped), which built `storage/doc_diff.py`, `read_state.last_read_at`, `versions.content_at_or_before`, the `?view=diff` render mode, and the inbox change-classification. Its own shipped note records the decision this bug overturns: *"mark-read stays unconditional so the empty-diff case never strands a misleading baseline"* — the empty-diff *note* is handled, but an unconditional bump on a plain view strands the baseline before the user ever sees the diff.

Touches **read-state** (owns `mark_read` / `last_read_at` / `unread_document_ids`) and **agent-submission-api** (`web/submit.py` returns `"url": "/doc/{id}"` with no `?view=diff`). The **skills-api-cutover** changed the path by which a doc is reached: pre-cutover Nigel arrived via the inbox link (diff-aware, appends `?view=diff` in `inbox.py:200`); post-cutover an agent relays the API's plain URL (diff-blind), which is what trips fault (1).

## Constraints

- **Chosen direction (Nigel, this session):** decouple the diff baseline from view-marking by tracking a separate acknowledged point — a `last_diffed_at` (or last-acknowledged version) that advances *only* when the diff is actually viewed — distinct from `last_read_at`, which keeps bumping on every view. Preferred over the alternative of auto-rendering diff on first view + deferring the read-bump.
- The diff is computed before `mark_read` within a single request (`doc_view.py:141` vs `:174`), so a direct `?view=diff` first-view already works; the fix must preserve that and additionally survive a plain view happening first.
- Two timestamps now drive two different signals: the inbox "new since last visit" predicate and the diff baseline. Decide deliberately whether the inbox keys off `last_read_at` or the new acknowledged point — they should not silently diverge in a confusing way.
- No retroactive fix for already-spent baselines (e.g. doc 307): once `last_read_at` is past the update, the v1→v2 diff can only be reconstructed by hand from version history.
- Likely needs a migration for the new column/table; `flagged-inbox-diff` shipped with no migration, so this would be the first schema change in this area.
- Quick-win companion (separable): have `put_document`/`get_document` return `/doc/{id}?view=diff` when there's a prior version, so agent-relayed "look at it" URLs open in diff mode.

## Open questions

- What exactly advances the acknowledged point — viewing `?view=diff` at all, or only viewing it when there *were* changes to show? What about the "formatting only" / no-textual-change case?
- Should the inbox's unread/changed signal continue keying off `last_read_at`, or off the new acknowledged point? Which gives the "haven't I already seen this?" behaviour Nigel actually wants?
- Granularity: a single timestamp vs a per-version acknowledged marker. Does acknowledging one update correctly handle a *second* update arriving before the first was diffed?
- Should the plain `/doc/{id}` view auto-redirect to diff mode when unacknowledged changes exist, or just keep the diff available behind the toggle? (Decision leaned toward keeping the baseline alive rather than forcing the redirect, but the UX is open.)
