# tracker-lifecycle-transitions — Context

## Problem space

The tracker's lifecycle is one-directional and incomplete. `FEATURE_STATUSES` is `(available, in_progress, done)`, and the only transitions are `claim` (available→in_progress) and `ship` (in_progress→done). Two moves that came up in real use have no path:

- **Park a feature.** Setting a feature deliberately aside — finalised or partway, but not being worked on and not abandoned — has no representation. Hit while working a kea feature (`review-severity-recalibration`): the user noted "we don't have a way in the system to mark something as parked", so we fell back to jamming a `PARKED` banner into the top of the context and requirements doc bodies. The banner is invisible to the tracker, so the feature still reads as in-progress (or available) in every view.
- **Release / unclaim.** Demoting an in-progress feature back to available — the inverse of `claim` — has no API or CLI path. To move `review-severity-recalibration` back to Available after parking it, the only option was a direct hand-edit of the SQLite `features` row (`status='available', owner=NULL`) via the app's connection helpers, plus a manually-inserted `feature_released` event for history. That is exactly the hand-editing the agent-submission tracker work set out to eliminate.

## Related work

**`tracker-drop-verb` (Available)** is adjacent and must be reconciled with this: it proposes a durable drop/archive path — "a drop/archive verb + an archived status the Available bucket excludes, or a delete endpoint" — so a dropped feature stops re-surfacing as available on every `features.md` merge-export. That is *terminal removal*; this feature is about *non-terminal* lifecycle moves (a deferred-but-alive `parked` state, and a `release` reversal of `claim`). The two overlap on "a status the Available bucket excludes" and should be designed together — see open questions.

The transition functions and their contract live in `storage/tracker.py` (`claim_feature`, `ship_feature`, `FEATURE_STATUSES`); routes are wired in `web/tracker.py` + `web/app.py`; status buckets are read in `web/project_page.py` and `storage/inbox.py`. The mutation contract established by `agent-submission-tracker-ops` (idempotent no-op on redundant transition, 409 on invalid, SSE broadcast only on real change, status invariant held in code not a DB CHECK) is the pattern any new transition should follow.

## Constraints

- Follow the existing mutation contract: redundant transition → idempotent no-op (no event); invalid transition → 409; SSE broadcast only when state actually changed; emit an event row for history.
- Every status the Available/inbox buckets must exclude has to be handled in *all* the read sites (`project_page.py`, `inbox.py`) and in the `features.md` merge-export, or it re-surfaces — the same leak `tracker-drop-verb` was created to fix.
- Status is enforced in code, not a DB `CHECK` (a full SQLite table rebuild wasn't judged worth it); a new status means updating `FEATURE_STATUSES` and any migration/backfill, not a constraint.
- The feature-* skills assume the current verbs (`capture`/`claim`/`ship`); a release verb and/or parked status may want skill-side affordances (e.g. `/feature` routing, a park action) — but the substrate (API + storage) is the in-scope core, mirroring how `agent-submission-tracker-ops` scoped itself to the additive substrate.

## Open questions

- Are `parked` and the `archived`/dropped state from `tracker-drop-verb` one status or two? Parked = deliberately deferred, expected to maybe resume; archived/dropped = removed so it stops re-surfacing. They share "excluded from Available" but differ in intent and in whether the feature should remain discoverable.
- What transitions are legal into/out of `parked`? Likely available→parked and in_progress→parked, and parked→available (resume). Does parked preserve `owner`, or clear it like release does?
- Is `release` a distinct verb, or just `claim`'s inverse exposed as `unclaim`? Naming should fit the existing capture/claim/ship vocabulary.
- Should this be merged into `tracker-drop-verb` as one "tracker lifecycle completeness" feature, or stay separate and land after it? They touch the same code and read sites.
- Does parking belong in the doc bodies at all (the banner we used) or purely as tracker state — i.e. should a parked feature's docs carry any marker, or is the status the single source of truth?
