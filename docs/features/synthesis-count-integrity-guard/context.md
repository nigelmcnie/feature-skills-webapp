# synthesis-count-integrity-guard

## Problem space and motivation

The calibration loop treats a **blank or absent synthesis response as "agreed with the take."** That assumption is load-bearing — it's what lets the review/plan/requirements skills auto-apply items the developer didn't engage with and proceed without waiting. But it silently conflates two very different states: *"the developer saw this and agreed"* versus *"the developer never saw this."*

The `_FeedbackParser` sibling-drop bug (fixed in PR #37) made the second state real and undetected: the webapp's `parse_feedback_items` dropped sibling items per tier, so the developer was shown only a subset of the authored items, and the resulting sparse submission read back as blanket agreement — genuine "needs your input" decisions never reached the developer, and the skill proceeded as if they had. The parser is now fixed, but **nothing asserts that the webapp actually rendered every item the skill authored** before blanks are trusted as agreement. So any future parsing/rendering regression (or a doc the parser silently mishandles) re-opens the same silent failure.

This is the **defence-in-depth half** of the PR #37 finding — proposed at the time, never built. It matters more as the workflow's direction of travel is for *more of the process to proceed automatically* on exactly this "blank = agreed" signal: the more that rides on it, the worse an undetected "never shown" masquerading as agreement becomes.

## Related work

- **The synthesis flow.** `feature-requirements` / `feature-review` / `feature-plan` author a feedback synthesis doc with N numbered items across three tiers; the webapp's `parse_feedback_items` (`storage/doc_render.py`) parses them for the native render; the developer submits; the skill reads responses back (now by logical key via `agent-submission-api`, or the path-keyed `/synthesis-response`) and treats blank/absent as agreement.
- **PR #37 / the flagged-inbox-diff retro finding.** Fixed `_FeedbackParser` dropping sibling items per tier on imbalanced body markup. That finding explicitly called for this systemic guard as defence-in-depth; this feature is that guard.
- **[agent-submission-api](../agent-submission-api/context.html) (shipped).** Added synthesis read-by-logical-key — the read side an assertion would hook into.

## Constraints and considerations

- **Cross-repo.** The *authored* item count is known to the skills (feature-skills repo); the *parsed/rendered* count is known to the webapp (feature-skills-webapp). The guard needs both sides to agree, so part of the work is deciding where the check lives and what the webapp exposes.
- **The webapp already parses items** (`parse_feedback_items`), so exposing a parsed-item count — or validating authored-vs-parsed at submit/index time — is the natural webapp-side hook.
- **Don't regress the "proceed without waiting" ergonomics.** The guard should fail loudly only on a genuine mismatch, never add friction to the common (correct) case where everything parsed.
- **Trust model unchanged.** Localhost, single-user, self-authored corpus — this is an integrity check on the workflow's own docs, not a security boundary.

## Links

- Parser fix: feature-skills-webapp PR #37 (`fix/feedback-parser-sibling-drop`) — the bug whose defence-in-depth this completes.
- Surfaced by: the `agent-submission-api` retro (retro-findings run 5).
- Parser: `feature_skills_webapp/storage/doc_render.py` — `parse_feedback_items` / `_FeedbackParser`.

## Open questions

1. Where does the assertion live — the **webapp** (reject/flag a doc whose parsed count ≠ an authored count it's told), or the **skills** (fetch the parsed count and compare before trusting blanks)?
2. How is the authored count conveyed to the webapp — an embedded meta tag / data attribute in the synthesis HTML (so the webapp can self-check at index time), or out-of-band when the skill reads responses?
3. On mismatch, hard-fail (block treating blanks as agreement, force the developer to look) or warn? What's the least-friction safe behaviour mid-flow?
4. Scope: synthesis docs only, or generalise to any parsed doc type where dropped content could be mistaken for absence?
