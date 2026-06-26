# surfacing-feedback-signal — Context

## Problem space

When the developer reviews a feedback synthesis doc, the only signal the workflow records per Needs-input (decision-tier) item is the response text: **blank** (agreed with the take) or **non-blank** (redirected / gave direction). Retros use this to judge *triage calibration* — whether items were surfaced to the right tier.

The problem: a blank conflates two opposite judgements about surfacing. "Agreed *and glad it was surfaced*" and "agreed *but this shouldn't have reached the decision tier*" both read as blank. So blanks carry no reliable signal about whether surfacing was *good*, yet retros have repeatedly inferred over-surfacing from them.

This surfaced concretely in the decouple-diff-baseline retro (2026-06-26): the developer noted he leaves items blank when he agrees, sometimes *glad* they surfaced — and pointed to the marker-advance item as one he was glad to see, despite it being a "foundational-but-technical" call (exactly the shape the over-surfacing theory says to downgrade). The recurring "over-surface technical decisions" finding (retro ids 3/7/14/37/42, and 47 this run) has been built on misread blanks; 47 was deferred and the recommendation withdrawn for lack of real data.

The ask: a lightweight, explicit good/bad-surfacing signal per decision-tier item, so calibration can be analysed from real data instead of inference.

## Related work

Builds directly on the feedback-synthesis mechanism: the synthesis form (rendered from feature-skills' `feedback-template.html`) and the `synthesis_responses` table + synthesis `GET`/`POST` API in feature-skills-webapp (`web/synthesis.py`), which today store one row per item with a `response` and a `routine_flag`.

It is the concrete resolution the recurring calibration finding has been asking for — retro finding **id 37** ("Triage-criterion calibration counts confirmations but never disproof") names exactly this gap; the deferred **id 47** and its correction (recurs_from 37) point here. Related calibration findings: ids 3, 7, 14, 42.

## Constraints

- **Cross-repo.** The control is rendered by feature-skills' `feedback-template.html` (the synthesis form HTML/JS, including the "Copy responses" payload shape); the signal is stored and served by feature-skills-webapp (`synthesis_responses` + the synthesis API). Both repos move together — the same coupling that prior findings (e.g. id 45) flag as a drift risk.
- **Scope it to the decision tier.** The thumbs signal is about *Needs-input* (and arguably middle-tier Feedback) items — "was surfacing this here a good call?" Routine items already have their own flag mechanism.
- **Don't over-claim from it.** A thumbs-down without commentary is still a fair "wrong tier" signal; a blank stays deliberately ambiguous (no thumbs = no signal, not agreement-that-it-surfaced). The goal is data to analyse later, *not* to auto-change triage tiers.
- Three axes now coexist and must not be conflated: *agree/redirect* (response text), *was-surfacing-good* (the new thumbs), and the routine *flag*. Keep them orthogonal in storage and in any retro analysis.
- Backwards compatibility: existing synthesis rows have no thumbs value; absence must read as "no signal," never as a thumbs verdict.

## Open questions

- Does the thumbs apply only to Needs-input, or also to the middle Feedback tier (where a thumbs-up might signal "this was correctly *not* escalated")?
- Is two-state (up/down) enough, or is a third "neutral/agreed" state needed so blank-vs-explicit-agree is itself distinguishable?
- Should a thumbs-down optionally prompt for a one-line reason, or stay frictionless (no commentary) so it actually gets used?
- How does the signal flow back for analysis — a retro-time query the calibration step runs, a column on `synthesis_responses`, or a separate table? (Storage shape is a requirements/plan question, but the analysis consumer should be named.)
- Does the same control belong in the inline review/plan triage (which now also has a "Need your call" vs "Applying" split in chat, not a form), or only in the HTML synthesis form? The chat-based triage has no place to record a thumbs.
