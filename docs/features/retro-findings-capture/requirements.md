# Retro findings capture

## Problem

Every `/feature-retro` produces a small set of findings about the feature-development *process*. They come in shapes: **quick wins** (a confident skill/template tweak, applied immediately), **worth-a-discussion** items (a larger process change or a feature candidate, talked through inline), and **loose ends** (session artefacts to clear).

Only the quick wins leave a trace: they land as code and survive in git. The worth-a-discussion findings — the most valuable ones, by the skill's own reckoning — evaporate when the session ends. Nothing records that they were raised, whether they were acted on, or whether they have come up before.

The consequence is that the retro cannot see its own history. A process problem that resurfaces every few features reads as brand-new each time, because the previous retro that flagged it left no durable record. The continuous-refinement loop the workflow is built around can only compound if each retro can stand on what prior retros found. Today it starts from zero.

This is the same principle the [synthesis-response-capture](../synthesis-response-capture/context.html) work turned on — "you can't continuously refine what you don't capture." There it applied to the developer's review answers; here it applies to the retro's own output, one layer up.

## Vision

When a retro raises a process finding, it is recorded against the project so the next retro can see what was flagged before, recognise what has recurred, and surface only the patterns that keep coming back — instead of re-discovering the same friction every session.

## User stories

1. As the retro agent
  I want to record this session's discussion-class
        findings against the project, automatically, at the end of the retro
  A

  on

  surfaces "the plan review asks about phase ordering
        every time". Rather than the point vanishing when the chat closes, the
        agent posts every discussion finding to the webapp at retro end, tagged
        with the feature and the run it came from — without waiting to be told
        which ones are worth keeping.
2. As the retro agent
  I want to read the project's prior open findings before
        I judge this session
  Before writing new findings for

  , the agent pulls the project's earlier findings,
        sees the same phase-ordering friction was raised two features ago, and
        records the new finding as a recurrence of that original rather than as a
        fresh observation.
3. As Nigel, in the retro session
  I want the agent to tell me, then and there, when a
        finding has come up before
  As the retro raises the phase-ordering point, it
        adds "this echoes a finding from

  two features ago" —
        so I can weigh it as a pattern in the moment, not discover the repetition
        later in the inbox.
4. As Nigel, reviewing the process
  I want to see which process findings are still open and
        which keep recurring
  In the inbox, a recurring finding stands out — it's
        been raised across three features and never resolved — which is the signal
        that it's worth promoting to an actual feature in the tracker.
5. As Nigel, triaging
  I want to mark a finding as actioned, deferred, or
        rejected so it stops nagging
  A finding led to a skill change last week; Nigel
        marks it actioned so it drops out of the "still open" view. Another he
        simply disagrees with — he marks it rejected, so it isn't re-raised as a
        recurrence next retro.

## Data model

A retro finding is a structured record with a lifecycle, not a prose document. It needs to be queried individually (by project, by status, by the finding it recurs from), so it lives in its own table rather than as a rendered HTML document in the existing `documents` / version model.

Findings are produced by a **retro run** — a first-class record of one `/feature-retro` invocation, carrying the project, the originating feature, and when it ran. Making the run a real entity (rather than a loose tag on each finding) is what gives the capture path a stable identity: re-running a retro replaces that run's findings instead of duplicating them.

A finding belongs to a run, and through it to a **project** — the unit across which recurrence is meaningful. The originating **feature** is recorded as a tag rather than a hard reference, so a retro can capture findings even for a feature the webapp hasn't ingested. A finding carries enough to be understood standalone — a short title, the evidence, and the proposed change or the question to discuss — mirroring the `/feature-retro` output format.

Two independent dimensions describe a finding, and the draft's original mistake was conflating them:

- **Status** — a developer-driven triage lifecycle: open, actioned, deferred, or rejected. This is what someone decides to *do* about a finding.
- **Recurrence** — an agent-observed property: a finding may record a **recurrence link** to the earlier finding it restates. "Recurring" is not a status; it is derived from these links (a finding with others pointing at it has recurred). Links form a star around the canonical original — every recurrence points at the first occurrence — so recurrence depth is a simple count, not a chain to walk.

Status changes are audited (when and to what), so a finding's triage history is recoverable. The first-raised timestamp is what drives the "long-open" prominence signal in the surfacing phase.

This sits alongside the existing `projects` → `features` spine. Out of scope for the data model: the quick-win findings (they live in git) and the global `/retro`'s output (it already persists as CLAUDE.md, settings, and memory).

## Technical approach

The webapp is the store and the surface; `feature-skills` (the `/feature-retro` skill) is the writer and reader. The contract between them is a small HTTP surface, in the same spirit as the existing synthesis-response endpoints.

### Write path

At the end of a retro, the skill posts the run's discussion-class findings for a project in one request: the run (identified so it can be re-posted idempotently), and each finding with its title, evidence, proposed change, originating feature tag, and — where the agent judged recurrence — a reference to the prior finding it recurs from. Re-posting a run **replaces** that run's findings rather than appending, inheriting the idempotency the synthesis endpoints get for free. The project is identified by name and must already exist (resolve-or-reject), matching how the synthesis endpoints treat a missing document.

### Read path

Before judging a session, the skill queries the project's still-relevant prior findings — those not actioned or rejected — so it has the history in hand when deciding whether something is new or a repeat. Crucially, the read path returns finding **ids**; the next write references one of those ids as the recurrence link. That read-returns-ids-then-write-cites-an-id round trip is the load-bearing mechanic of the whole loop.

### Recurrence is agent-judged, not computed

We deliberately avoid building fuzzy or semantic matching in the webapp. The retro agent is already reading the prior findings as prose; asking it to say "this is the same as #14" is both cheaper and better than an embedding pipeline, and keeps the webapp a dumb, reliable store. The cost is that recurrence is only as good as the agent's judgement — acceptable at this scale. (With multiple machines writing to one hosted DB, two retros could independently draw divergent links; that too is acceptable at this volume and consistent with the agent-judgement stance.)

### Surfacing

Findings surface to the developer for review and triage, with recurring and long-open items given prominence (they are the signal; a graveyard of stale findings is the failure mode), and the developer can change a finding's status. The intended home is the existing inbox, building on the server-rendered direction established by [server-rendered-docs](../server-rendered-docs/context.html) rather than introducing a separate surface. *How* a non-document entity is woven into an inbox built around `documents` — a unified view, a separate panel, or a synthesised pseudo-document — is an open design question for the plan, and materially affects the surfacing phase's size.

### Trust boundary

The write and read endpoints are localhost-only and unauthenticated, exactly like the existing endpoints — the current self-hosted, single-user model. Authentication for the hosted, multi-machine, shared-at-Sharesies future the context anticipates is a known follow-up, explicitly out of scope here.

### Relationship to the cross-agent write contract

The [agent-submission-api](../agent-submission-api/context.html) feature will later generalise "an agent submits structured data by logical key". Retro findings could eventually ride on that, but this feature defines its own focused endpoints now so it is shippable independently and not blocked on that larger contract.

## Scope & non-goals

In scope: capturing and querying discussion-class retro findings, tracking their triage status and recurrence, and surfacing them for review.

Explicitly **not** in scope:

- **Quick-win findings** — they already survive as code in git.
- **Global `/retro` output** — already persists as CLAUDE.md edits, settings, installs, and memory.
- **Authentication** — the multi-machine/hosted future; the endpoints stay localhost-only for now.
- **Recurrence-trend signal** — surfacing "raised in N retros" as an explicit trend and feeding it back into the retro prompt. Split out to a separate tracked feature, to be designed once this one has run in anger (see Delivery phases).
- **Promotion to a tracked feature** — a one-click "promote a recurring finding → feature candidate" (via the existing `feature-context` path) is a natural future affordance, noted here but not built.

## Alternatives considered

1. Store each retro run as a versioned HTML document
  Source: the documents / document_versions model in this repo
  Reuses the existing doc plumbing, but a monolithic
        per-run document can't be queried per-finding, carries no per-finding status,
        and makes recurrence detection ("has this come up before?") a full-text
        reading problem rather than a query. The whole value is queryable findings,
        so a structured table wins.
2. Per-feature findings log instead of project-scoped
  Source: open question in the captured context
  Recurrence is a cross-feature phenomenon — the point
        is to notice a problem resurfacing across different features. A per-feature
        log can't see that. Findings are project-scoped, merely tagged with the
        feature they arose from.
3. "recurring" as a fourth status value
  Source: review round 1
  The original draft made status one of {open,
        actioned, deferred, recurring}. But recurrence (an agent-observed property)
        is orthogonal to triage state (a developer decision): a finding can be both
        recurring and still open, or recurring and actioned. Folding them into one
        column forces a false choice. Recurrence is now expressed by links; status
        is the triage lifecycle alone.
4. Compute recurrence in the webapp (keys or embeddings)
  Source: open question in the captured context
  Exact-match keys are too brittle for prose findings;
        embeddings are far too much machinery for the volume involved. The agent is
        already reading the priors — let it make the call and just store the link.
5. Also persist global

  output
  Source: open question in the captured context
  Its findings already become durable as CLAUDE.md
        edits, settings, installs, and memory entries. Persisting them here would
        duplicate state and blur the two retros' lanes. Out of scope.

## Delivery phases

### Phase 1 — Findings store + capture/query contract

The run and findings tables and the HTTP endpoints to record a retro run's findings for a project (idempotent re-post) and to query the project's prior still-relevant findings, with ids returned so a later write can cite a recurrence link. This alone closes the loop: the next `/feature-retro` can read what earlier retros flagged. Testable end-to-end — write findings for a project, read them back (with ids), write a follow-up run that links a finding as recurring, confirm the link survives and that re-posting a run doesn't duplicate. No UI yet.

### Phase 2 — Surface and triage

Surface a project's findings for review, with recurring and long-open items prominent, and let the developer change a finding's status (actioned / deferred / rejected). This is what keeps the signal high — without it the store silently accretes a graveyard. The integration into the `documents`-centric inbox is a design question to settle in the plan. Testable: a recurring finding is visibly distinguished; marking one actioned or rejected removes it from the open view and from the next retro's query.

The recurrence-trend signal (surface "raised in N retros"; feed it back into the retro prompt) is deliberately **not** a phase here — it is split to a separate tracked feature, to be designed once Phases 1–2 have been used and the shape of real recurrence data is known.

## Indicative implementation notes

Carried forward for `/feature-plan`, not binding:

- The synthesis-response endpoints (`feature_skills_webapp/web/synthesis.py`) are the closest existing pattern for the write/read HTTP surface: validate before `BEGIN IMMEDIATE`, size-cap values, resolve-or-404 on a missing parent, broadcast on write.
- Idempotency mirrors synthesis's delete-then-insert, but keyed on the run rather than a document: re-posting a run deletes that run's findings and re-inserts. The agent supplies a run identifier (e.g. a generated id or timestamp) — `/feature-retro` has none today, so emitting one is part of the producer-side change.
- A new migration adds the run and findings tables; follow the existing numbered `storage/migrations/000N_*.sql` convention and bump `schema_version`.
- Status: `open | actioned | deferred | rejected`. Recurrence is a self-referential foreign key on the findings table pointing at the canonical original (a star, not a chain), so depth is a child count.
- Status changes should append to the existing `events` table (`ON DELETE SET NULL` precisely so audit history outlives its subject) rather than a new history table.
- Illustrative contract shape (exact schema is the plan's job): The `recurs_from` on write is an `id` the agent learned from a prior GET.
  ```json
  POST  → { "project": "feature-skills-webapp",
            "run": { "id": "<agent-supplied>", "feature": "doc-view", "ran_at": "…" },
            "findings": [ { "title": "…", "evidence": "…",
                            "change": "…", "recurs_from": 14 } ] }
  GET   → { "findings": [ { "id": 27, "title": "…", "status": "open",
                            "feature": "doc-view", "recurs_from": 14,
                            "recurrence_count": 3, "created_at": "…" } ] }
  ```
- The two-repo contract means the `/feature-retro` SKILL.md in `feature-skills` changes too: it currently says "don't… write anything" for discussion findings — this feature reverses that. It should now query priors at the start, surface recurrences to the developer inline, and post all discussion findings at the end. The endpoint shape is the joint contract to pin down in the plan.
- Cross-check against `agent-submission-api` so the endpoint shape here doesn't paint that later, more general contract into a corner.

## Design notes

Decisions captured during review iteration:

- **Round 1 — status vs recurrence are separate axes.** Status is the developer's triage lifecycle; recurrence is an agent-observed property derived from links. Not one combined column. (This is the modelling decision the rest of the data model hangs on.)
- **Round 1 — retro run is a first-class entity.** Chosen for idempotency: re-posting a run replaces its findings, so re-running a retro can't duplicate the store into a graveyard.
- **Round 1 — autonomous capture.** The agent posts all discussion findings at retro end rather than only developer-blessed ones — because you can't tell in the moment which will recur; inbox triage and the recurrence signal keep noise down, not pre-filtering at write time.
- **Round 1 — recurrence links form a star.** Every recurrence points at the canonical original, making depth a simple count.
- **Round 1 — feature is a tag, project is resolved by name.** A finding's feature need not have a webapp row (retro may run before docs are ingested); the project must exist (resolve-or-reject).
- **Round 1 — added a `rejected` terminal state** so a deliberately-declined finding isn't dishonestly "deferred" forever or re-raised as a recurrence.
- **Round 1 — recurrence surfaced in-session.** The highest- leverage moment to note "this was flagged before" is during the retro, not only later in the inbox.
- **Round 1 — Phase 3 (recurrence trend) split out** to a separate tracked feature; its right design depends on data Phases 1–2 will produce.
- **Round 1 — trust boundary stated.** Localhost-only and unauthenticated, as today; multi-machine auth is a named follow-up.

## Review decisions

**Round 1 (post-merge review).** The merged implementation matched the plan and requirements with no correctness issues — the migration, the idempotent replace-by-run capture, the `recurs_from` self-run/cross-project guards (correctly returning 400, not 500), the no-op status audit suppression, template escaping, and the localhost trust boundary all verified. QC clean on main (ruff, ty, 452 tests).

- **Fixed:** the one gap — decision 6's "original re-posted later" branch (a child's `recurs_from` dropping to NULL when the original's run is replaced) had no direct test. Added `test_reposting_original_run_nulls_child_recurs_from` pinning the `ON DELETE SET NULL` behaviour (child survives with a null link, re-post returns 200 not a FK 500).
- **Declined:** factoring the duplicated `recurrence_count` COUNT subquery into a shared helper between the GET endpoint and the read model — the plan called this refactor optional and it isn't worth the churn.

**Round 2 (producer-side wiring).** Reviewing the `feature-skills` producer changes that close the contract.

- **Reversed:** the run-key obligation. The plan's HTTP contract specified a key *stable across re-posts of the same retro* (so a re-run replaces). The shipped producer instead mints a fresh key per `/feature-retro` invocation, and that's the chosen behaviour: it keeps the planner's and implementer's retros on one feature as distinct runs. Consequence (accepted): the webapp's replace-by-run-key idempotency only catches an in-session retry that reuses the key, never a genuine re-run — a re-roll appends a new run rather than replacing. Per-invocation keys judged the better trade-off than conflating distinct retros.
