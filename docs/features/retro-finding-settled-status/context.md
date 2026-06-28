# retro-finding-settled-status — Context

## Problem space

Retro findings have four states (`open`, `actioned`, `deferred`, `rejected`; `_ALLOWED_STATUS` in `web/retro_findings.py`). The start-of-retro GET returns only `open` + `deferred`, so the *only* way to stop a finding resurfacing every retro is to mark it `actioned` or `rejected` — both of which drop it from the feed entirely.

That leaves no good home for a finding the developer has **consciously decided not to act on because it is a known, accepted special case**. The recurring example is the “cross-repo phases” finding (~8 instances across features: #16×4, #23, #26, #36, #39, #41, #43, #57): it's intrinsic to feature-skills spanning the skills repo *and* this webapp, not a general development problem. `deferred` keeps nagging (still in the feed); `rejected` silences it but then a future retro can't see it, doesn't recognise the repeat, and **re-mints it fresh as `open`** — so the developer dismisses the same theme every retro. The recurrence machinery can represent “raised again” but not “known & accepted — don't re-raise.”

Surfaced in the api-coherence orchestrator retro (2026-06-28): Nigel noted the cross-repo finding keeps recurring and he keeps not wanting to act on it.

## Related work

- **retro-findings-capture** — built the status axis (open/actioned/deferred/rejected), the recurrence axis (`recurs_from`/`recurrence_count`), and the `POST /retro-findings/{id}/status` mutation. This feature extends that model.
- **The `/feature-retro` skill** (feature-skills repo) — reads `open` + `deferred` at retro start, surfaces recurrence inline, and posts discussion-class findings citing `recurs_from`. It would need to learn the new state's semantics (render an accepted/settled theme as “known, don't re-raise”).
- **retro-recurrence-trend** (Available) — the related recurrence-depth signal; worth considering together since both shape how repeats are surfaced.
- The state set + GET filter live in `feature_skills_webapp/web/retro_findings.py` (`_ALLOWED_STATUS`, line ~15; `status IN ('open','deferred')`, line ~196).

## Constraints

- Cross-repo: the webapp owns the status set + GET filter; the `/feature-retro` skill (feature-skills repo) owns how the new state is read and rendered. Both must move together.
- Don't break the existing four states or the `recurs_from` validation / events.
- Trust model unchanged: localhost, single-user, no auth.
- The distinguishing requirement from `rejected`: the new state must *stay visible* to the retro-start read (so a recurrence is recognised, not re-minted) while being clearly marked not-actionable — the opposite of how `rejected`/`actioned` drop out of the feed.

## Open questions

- Name for the state — `settled`? `accepted`? `wont_fix`? `by_design`? (Each carries a slightly different connotation.)
- Mechanism: a fifth status the GET returns-but-flags, or a separate “acknowledged” shelf the retro reads distinctly from the actionable feed?
- How does it interact with `recurrence_count` — does an accepted finding still accrue recurrence (evidence the pain persists, maybe a trigger to revisit), or freeze?
- Skill behaviour: on seeing an accepted theme recur, should `/feature-retro` cite it via `recurs_from` and explicitly *not* re-raise it as a new actionable item?
- Is a transition back to `open` needed if circumstances change (the special case stops being special)?
- Is this general, or should “accepted” themes be scoped per-project (this one is feature-skills-specific)?
