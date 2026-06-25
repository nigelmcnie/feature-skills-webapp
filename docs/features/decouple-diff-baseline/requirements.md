# decouple-diff-baseline — Requirements

## Summary

The webapp can show a section-aware **diff** of a document — "here's what changed since you last looked." The problem: the act of opening a document destroys its own diff.

Concrete case that surfaced this (doc 307): an agent revised a requirements doc after a feedback round, then told the developer to look at it. They opened it, read it, then clicked *View changes* — and got *"No text changes since you last read this document."* That was plainly false; the doc had just been rewritten.

The cause is that the diff is measured against a single timestamp, `last_read_at`, which is stamped to "now" on *every* page view. Opening the doc to look at it moved that timestamp *past* the update, so the diff had nothing left to compare against. The same stamp also drops the document out of the inbox's "new since last visit" list — so a single casual open both blanks the diff and clears the unread flag, before the developer has actually seen what changed.

This feature separates "I've looked at this document" from "I've seen what changed in it." We add a distinct marker that only advances when the developer actually views the diff. Opening a document normally — however many times — no longer spends the diff or clears the change flag. The diff and the inbox both keep showing the change until the developer has genuinely reviewed it.

## Scope

In scope:

- A per-document **acknowledged-change marker** recording the latest version whose changes the developer has actually been shown (via the diff view), separate from `last_read_at`.
- A single, centrally-defined notion of **"has unreviewed changes"** (latest version > acknowledged version) and of the **diff baseline** (the acknowledged version's content), consumed by the diff view, the inbox, and the plain-view banner alike.
- Computing the diff baseline from that marker instead of from `last_read_at`, so plain views never empty the diff.
- Advancing the marker only when the diff view is rendered — not on a normal read.
- Keeping a changed section doc surfaced in the inbox until its diff has been viewed, rather than clearing on any open — including the inbox's *surfacing* query, not just the card label.
- Making the change discoverable from a plain view (a non-blocking "changed since you reviewed it" banner; relayed/agent-facing links for changed docs pointing at the diff).
- A one-time migration backfill so existing documents keep today's diff behaviour rather than appearing wholly new.
- A review of the existing test coverage for read-state, the diff view, and the inbox, extended where it helps pin the new behaviour and keep the change low-risk.

## Vision

Looking at a document never costs you the diff: a change stays visible — in the doc and in the inbox — until you've actually reviewed what changed, no matter how many times you open it first.

## Non goals

- **Retroactively repairing already-spent baselines.** Doc 307's `last_read_at` is already past its update; no code change reconstructs that diff. It can only be eyeballed from version history. The fix is forward-looking (existing docs are handled by a one-time backfill, not a repair).
- **Redesigning read-state wholesale.** `last_read_at` keeps its current meaning and its current role for opaque docs (feedback, the tracker) and the "Mark all read" action. `unread_document_ids` — the generic "any activity since last read" signal — stays on `last_read_at` and is out of scope; only the section-content-change path moves to the new marker.
- **Forcing the diff on the developer.** We are not auto-redirecting every plain open into diff mode; the full document stays the default view. Discoverability is the non-blocking banner.
- **Changing how the diff itself is computed or rendered.** The section-aware text diff from `flagged-inbox-diff` is reused unchanged; only the choice of *baseline* moves.

## User stories

1. As a developer told by an agent to "look at the updated doc"
  I want to open it, read it, and still be able to see exactly what changed
  I open the doc from the URL the agent gave me, skim the current text, then click

  — and I see the highlighted insertions and deletions from the revision, not a "no changes" message.
2. As a developer working through my inbox
  I want an updated document to stay flagged until I've actually reviewed the change
  A doc shows "Updated — Technical approach changed." I click in, glance at it, and navigate away without opening the diff. It's still in my inbox afterwards, because I never saw what changed — it only clears once I've viewed the diff.
3. As a developer who opens a doc before reviewing several updates
  I want the diff to span everything since I last reviewed, even across multiple revisions
  An agent updates a doc twice while I'm busy. When I finally open the diff, it shows the combined change since the last time I actually reviewed it — not just the most recent edit, and not nothing.
4. As a developer on a plain document view
  I want a clear nudge when the doc has changed since I last reviewed it
  I open a doc that was revised since my last review. A small banner tells me it changed and offers

  , so I don't have to guess whether the diff is worth opening.

## Data model

Read-state is already one row per document, keyed by `document_id`, holding `last_read_at`. This feature adds one nullable field alongside it: the **acknowledged version** — the `version_num` of the document version whose changes the developer was last shown in the diff view.

- It points at a row in the existing per-document version history; it is always less than or equal to the latest version.
- A document has *unreviewed changes* precisely when its latest version is greater than its acknowledged version. "Latest" is read live (the max version number, already computed where needed) — no second stored column.
- **Migration backfill:** existing read-state rows get their marker set to the version at-or-before each document's current `last_read_at` — reproducing today's behaviour, so already-read docs keep a sensible prior and only genuine new changes light up. This is a one-time, forward-only backfill in the migration.
- **Null marker** (a freshly-created doc, never read or diffed) means no prior: it reuses today's no-prior handling unchanged — the diff view shows the native doc with a "nothing to compare" note, not a synthetic all-inserted diff.
- The logical-key collision merge of read-state rows must carry the new field (keeping the higher acknowledged version), so a merge never silently drops it.

A version number is chosen over a second timestamp deliberately (see Alternatives): it is unambiguous about *which* revision was seen, and it makes "two updates before I reviewed" behave correctly with no timestamp-ordering subtlety.

## Technical approach

**One definition, three consumers.** The heart of the change is a single, centrally-defined rule — "this document has unreviewed changes" (latest version > acknowledged version) — and a single diff-baseline accessor (the acknowledged version's content). The diff view, the inbox, and the plain-view banner all derive from that one definition rather than three hand-synced copies, so they move in lockstep by construction. The diff baseline thus shifts from "the version at or before `last_read_at`" to "the acknowledged version."

- **Diff view:** reads the acknowledged version, fetches that version's content as the prior, and diffs it against current. After rendering the diff, it advances the acknowledged version to the current latest — and it does so on *any* render of the diff view (real diff, formatting-only, or no-prior), so a doc never stays stuck "unreviewed" after you've looked. `last_read_at` continues to be stamped on every view as today; it simply no longer governs the diff. The advance shares the existing per-view write path (a GET already writes via `mark_read`) and is idempotent under concurrent diff views (each sets the marker to the same latest).
- **Plain views** of a section doc do *not* touch the acknowledged version, so the diff survives any number of opens.
- **Inbox:** a changed section doc must keep surfacing until its diff is viewed. This requires changing the inbox's *surfacing* query (which today gates on an event newer than `last_read_at` and so evicts the card on any open), not only the card-label classification. The surfacing rule becomes a hybrid: section docs surface while they have unreviewed changes; feedback/comment-driven signals keep using `last_read_at` — deduplicated so a doc surfaces once.
- **Banner / relayed links:** a section doc with unreviewed changes, viewed plainly, shows a non-blocking banner offering the diff. Changed docs' agent-facing links point at the diff view so a relayed "go look" lands on the change. Both keyed off the same "has unreviewed changes" definition.
- The existing timestamp accessor (`content_at_or_before`) is *retained* — it still serves the unchanged comment/feedback path; the new baseline-by-version accessor is added alongside it.

The pure diff machinery, the version store, and the diff render mode are all reused as-is. The work is in *which baseline is chosen*, *when the marker advances*, and *centralising the one predicate*, plus the migration.

## Testing

This ships as one change touching read-state, the diff view, and the shared inbox query, so behaviour must be pinned carefully. Start by reviewing the existing test coverage for those three areas and extend it where it helps lock the new behaviour in. Each behavioural test must be able to fail without the change (revert, see red, restore):

- Open a changed section doc plainly, then request the diff: the diff still shows the change (the regression that started this).
- View the diff, then request it again with no new edits: now it reports no changes — the marker advanced.
- The marker advances on a formatting-only and a no-prior diff view too (doc doesn't stay stuck unreviewed).
- Two updates land between reviews: the diff spans from the last acknowledged version to the latest, not just the most recent edit.
- A plain open of a changed doc leaves it in the inbox; viewing the diff clears it — exercised through the actual surfacing query, not just the label classifier.
- Feedback/comment-driven inbox surfacing is unchanged; no doc surfaces twice.
- Migration backfill: an existing read-but-not-diffed doc keeps today's prior (doesn't appear wholly new); a never-read doc stays null and uses the no-prior native path.
- `last_read_at`, opaque-doc, `unread_document_ids`, and "Mark all read" behaviour are unchanged.
- The collision merge carries the acknowledged marker.

## Alternatives

1. Keep the timestamp; defer the read-stamp until the diff is seen
  Source: investigation, this session (the two candidate fixes)
  Set aside in favour of a separate marker. Deferring

  bumps keeps overloading one field for two meanings and stays fragile to any view path that bumps it early. A dedicated marker is the cleaner separation of "looked at" from "reviewed the change."
2. Use a second timestamp (

  ) rather than a version number
  Source: context doc open question
  A timestamp reintroduces the ordering subtlety we are trying to escape (which version was actually current at that instant) and makes the "two updates before I reviewed" case ambiguous. An explicit acknowledged

  answers "what did I see" exactly.
3. Force every plain open of a changed doc into diff mode
  Source: context doc open question
  Rejected as too heavy-handed — the full document is the natural default for reading. A non-blocking banner gives discoverability without hijacking the view.
4. Phase the work (marker → inbox → polish) and/or ship the agent-URL fix first
  Source: review round 1
  Rejected in favour of one coherent change (developer call). The pieces are tightly coupled around a single predicate; splitting them risks a window where the relayed-URL "relief" is one-click-deep because the durable baseline hasn't landed yet. Delivered together, with careful behaviour-pinning tests instead of phase boundaries to manage risk.

Historical note: `flagged-inbox-diff`'s own requirements explicitly rejected a per-document last-seen version as "unnecessary," reasoning that `last_read_at` plus version timestamps already located the prior version. This feature is the deliberate reversal of that call — that baseline turned out to be spent by ordinary reading.

## Delivery phases

### Single phase — Acknowledged-version marker, durable diff baseline, inbox + banner in lockstep

Delivered as one coherent MR (developer decision, review round 1): the pieces all turn on the same acknowledged-version concept, so splitting them buys little and risks a half-fixed intermediate state. Risk is managed by pinning behaviour with tests rather than by phase boundaries.

The change comprises, in roughly this order:

1. Migration: add the nullable acknowledged-version field to read-state and backfill existing rows from the version at-or-before `last_read_at`.
2. Centralise the one predicate ("has unreviewed changes") and the baseline-by-version accessor.
3. Switch the diff view to baseline against the acknowledged version and advance the marker (to current latest) on any diff render; leave `last_read_at` stamping as-is.
4. Update the inbox *surfacing* query (the hybrid section-vs-feedback predicate) plus the card classification to the new signal.
5. Add the plain-view banner and point changed docs' agent-facing links at the diff view.
6. Carry the marker through the read-state collision merge.
7. Review and extend the existing read-state / diff-view / inbox test coverage to pin all of the above.

## Indicative notes

Carried forward for planning, not binding:

- The marker fits as a nullable column on the existing `read_state` table (one row per document); next migration in sequence after 0005 (i.e. 0006), forward-only.
- Touch points from the investigation: the diff branch in `web/doc_view.py` (where `content_at_or_before(last_read_at)` currently picks the baseline, and where `mark_read` runs after the diff is computed — the marker advance goes there); a baseline-by-version accessor alongside `versions.content_at_or_before` (which stays); read-state accessors in `storage/read_state.py` for reading/advancing the marker; the content-change branch of `classify_reason` *and* the surfacing SQL in `new_since_last_visit` in `storage/inbox.py`; and the collision merge `_merge_read_state` in `storage/versions.py`.
- The agent-URL piece is the `"url"` field returned by `put_document`/`get_document` in `web/submit.py`, which today hands back a diff-blind `/doc/{id}`.
- The "has unreviewed changes" predicate spans a Python check (diff view, banner) and a SQL fragment (inbox surfacing); centralise so the two can't drift even though they live either side of the DB boundary.

## Design notes

- **One phase, not three** (review round 1). The marker, the inbox surfacing change, and the banner/URL polish are tightly coupled around the acknowledged-version concept; delivered together with behaviour-pinning tests rather than phased, to avoid a half-fixed intermediate state.
- **Acknowledged *version*, not a second timestamp** — unambiguous about which revision was seen; handles multiple updates before review cleanly.
- **Migration backfill derives from `last_read_at`** (review round 1, option b) rather than nulling existing rows, so the existing backlog keeps today's diff behaviour instead of appearing wholly new on first run.
- **Marker advances on any diff-view render** (review round 1), including formatting-only and no-prior, so a doc never stays stuck unreviewed after you've looked.
- **Single source of truth** (review round 1): the "has unreviewed changes" rule and the diff baseline are each defined once and consumed by the diff view, inbox surfacing, and banner, so the three move in lockstep by construction — even across the Python/SQL boundary.
- **Phase 2 blast radius caught in review:** the inbox's surfacing query (not just the card label) gates on `last_read_at`, so it had to move to the new signal too; this is the widest-reach part and the main reason coverage is reviewed and extended.
