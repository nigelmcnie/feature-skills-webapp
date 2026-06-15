# flagged-inbox-diff

## Problem

The inbox's **"New since last visit"** list is genuinely useful when it surfaces a doc as part of a workflow step — a requirements doc created, a review doc posted. But during *implementation* and *review*, the same docs get re-touched, re-bubble to the top of the list, and you click in only to think *"haven't we done this already?"* — there's no sign of *what* changed, so you re-read the whole doc to find out. That is the original pain the entire structured-content arc (F1 `versioned-content-store`, F2 `server-rendered-docs`) was built to make fixable.

Half of that pain is already gone. F1's walker cuts a version and emits an `updated` event *only when content actually changed* (`walker.py`: it compares the serialised section content), so an mtime-only touch or a whitespace re-save that serialises identically produces no event, no version, and no re-bubble. The empty, noise re-surfacing is dead at the root.

What remains is the *legitimate* re-bubble: the content genuinely changed, or there was comment activity on the doc. The card shows neither which it was nor what moved, and the doc view has only one mode — the full render. The reader still pays a full re-read for a two-line change.

## Vision

A re-surfaced doc stays in the inbox flagged with why it came back, and — when the reason was a content change — opens straight to a section-aware diff of just what changed since you last read it, one toggle away from the full doc.

## Scope

**In scope.** The section-structured doc types that actually re-bubble in "New since last visit": `context`, `requirements`, and `plan`. The three pieces are (1) a *reason flag* on the inbox card, (2) a *section-aware diff view* of the open doc, and (3) *defaulting* the card's link to the diff when the re-bubble was a content change with something to diff against.

**Event coverage.** The re-bubble reason is derived from the doc's events newer than the read baseline. Beyond `created`/`updated` (content) and `comment_submitted`/`comment_integrated` (interaction), the walker also emits `reactivated` (a doc returning from `missing`) and `archived`. A `reactivated` event rides alongside a fresh content version, so it folds into the content-change class and diffs like any update. `archived` docs leave "New since last visit" entirely (the query is `status='active'` only), so they can't surface there — no special handling needed. No new card vocabulary; just complete event mapping.

**Out of scope / deferred.** Actor-based semantic reasons ("an agent applied feedback" vs "you revised it") — `actor` is uniformly `"importer"` until `agent-submission-api` lands, so the reason is derived structurally now and the semantic layer is a forward-compatible enrichment, not a blocker. An inter-version stepper (browsing every intermediate version) is not needed; the diff always collapses to *last-seen → current*. A diff for opaque-body doc types (feedback, the tracker) is not built in v1 — see the data-model note on why those don't reach the inbox's re-bubble path today.

## User stories

1. As a developer mid-feature
  I want each re-surfaced card to tell me why it came back
  An implementing agent revised the plan's "Technical approach" section. The card reads "Updated — Technical approach changed" instead of a bare "Plan", so I can decide at a glance whether to open it.
2. As a developer reviewing a changed doc
  I want a content re-bubble to open directly on a diff of only what changed
  I last read the requirements two versions ago. Opening it from the inbox shows the sections that moved, with an inline highlight of the changed text, and leaves the untouched sections marked unchanged — no full re-read.
3. As a developer wanting full context
  I want to toggle between the diff and the full rendered doc
  The diff shows a changed section but I want the surrounding argument. One click switches to the full render, and one click back to the diff.
4. As a developer who has caught up
  viewing the diff to advance my "last seen" baseline
  I read the diff and leave. The card drops off "New since last visit" and won't flag again until the doc changes once more — exactly as opening the full doc already does.
5. As a developer whose doc only got comments
  a comment-only re-bubble flagged as such, opening to the normal view
  I left click-to-comment notes on the requirements but the content didn't change. The card reads "Comments added" and opens the full render — there's no content delta, so it doesn't land me on an empty diff.

## Data model

**No new tables and no migration.** Everything needed is already stored:

- **Versions** (`document_versions`): each carries the serialised structured content and a creation time. The diff pair is *prior* = the most recent version you'd already seen (the latest one no newer than your read baseline), against *current* = the latest version. Anchoring on the existing read timestamp means we never persist a per-document "last seen version". **First sighting** is defined as *no version at-or-before the baseline* — this covers both a brand-new doc and a never-opened-but-already-versioned one; both have nothing to diff and show the full render, labelled "New".
- **Read state** (`read_state.last_read_at`): the existing "what I last saw" anchor — already what pins both the inbox's unread predicate and the diff's baseline.
- **Events** (`events`): the re-bubble reason is derived from the events newer than `last_read_at` for the doc — a `created`/`updated` content event versus `comment_submitted`/`comment_integrated`.

The inbox card model (`InboxCard`) gains a **reason** (a structured classification plus a humanised label) and enough to know whether to deep-link to the diff. No other read model changes.

**Why feedback and the tracker stay out of the diff path.** "New since last visit" already excludes feedback docs (they live in "Awaiting your input") and the tracker (it has no feature, so the inner join drops it). Synthesis submission, notably, writes *no* event row today — it only broadcasts — so it cannot drive a re-bubble in any case. The set of docs that genuinely re-surface in "New since last visit" is therefore exactly the three section-structured types, which is why the v1 diff only needs to handle section docs.

## Technical approach

**Derive the reason structurally, in the read model.** When the inbox builds a "New since last visit" card, classify the re-bubble from the doc's events since the read baseline: a first sighting (no prior version) reads as *new*; a newer content version (including a `reactivated` event, which carries one) reads as a *content change*; events that are only comment activity read as *comments added*. For a content change the card reports how many sections moved — counting added, removed, and changed alike under one "changed" banner, since the reader only cares that N spots shifted (the diff *view* still distinguishes the three). Section names are humanised from the existing per-type manifest (key → label), falling back to a prettified key; the label names up to a couple of sections and elides the rest as "+N more". We derive this read-side rather than recording it on the version-cut event: at single-user inbox scale, decoding two versions per changed card is cheap, and it keeps the logic in one pure, testable place with the walker untouched. Recording the changed-section set on the `updated` event is a forward-compatible optimisation if the list ever feels slow.

**The diff is a pure, section-aware function.** Mirroring the existing pure modules (`doc_content.py` parses, `doc_render.py` renders), a new pure module computes a diff over two parsed contents: match sections by `key` into added / removed / changed / unchanged, and for a changed section diff the *rendered text* of the body — not the raw HTML fragment, because the bodies are opaque trusted HTML and a raw diff would drown the reader in markup noise. (Matching purely by `key` means a renamed section reads as one removed + one added rather than one changed; that's an accepted simplification — a rename genuinely is a structural change worth seeing as such.) A first sighting has nothing to diff and falls back to the full render.

**Empty textual diff despite a content version.** The version gate is byte-level, not semantic, so a formatting-only re-save (attribute reorder, whitespace) can cut a version whose *rendered text* is unchanged — a card that says "Updated" with nothing to show. In that case the diff view shows the full render with a small "no textual changes (formatting only)" note rather than an empty diff pane, so the reader is never left staring at a blank delta.

**The diff is another server-rendered mode of the same doc page.** Because F2 removed the iframe and the doc renders natively, the diff view is just a second rendering mode of `/doc/{id}`, reusing the existing section renderer for unchanged sections, with a toggle between the diff and the full render. Viewing the doc stamps the read baseline exactly as it already does for every view — the empty-diff fallback above means you're never marked read while looking at a blank delta. We deliberately keep that stamp unconditional rather than gating it on "the delta was non-empty": adding that state buys little on a single-user local tool and complicates the one genuinely hard-to-reverse signal in the system (see Design notes on the read-state trust boundary).

**The card deep-links to the right landing.** When a card's reason is a content change with a prior version to diff against, its link targets the diff view; otherwise (new doc, comment-only) it targets the full render. This is the end-to-end payoff: click the flagged card, land on the delta.

## Alternatives considered

1. Diff the raw HTML fragment instead of rendered text
  Source: context doc open question
  Section bodies are opaque trusted HTML. Diffing the raw fragment would surface tag and attribute noise as if it were content change. Diffing the rendered text is more readable and the renderer already knows how to produce it.
2. Persist a per-document "last seen version_num"
  Source: context doc open question
  Unnecessary. Versions carry

  and read-state already stores

  , so the prior version is "latest with created_at ≤ last_read_at" — no new column, no extra write on view.
3. A separate diff sub-route (

  )
  Source: context doc open question
  Leaning against, in favour of a query parameter on the existing route, so one handler owns render-mode selection and shares the breadcrumb / mark-read / sibling-nav plumbing. Flagged for confirmation in delivery.
4. Wait for meaningful

  values before flagging
  Source: context doc constraint
  Would block the whole feature on

  . The structural reason (which sections moved, content vs comments vs new) is useful on its own; actor-based semantic reasons light up later without rework.
5. Record the changed-section set on the version-cut event
  Source: round-1 review
  The walker already writes

  on

  events, so the changed sections could be computed once at version-cut and read cheaply by the inbox. Deferred: at single-user scale the read-side recompute is cheap, and keeping it read-only leaves the walker untouched. Promote to this if the inbox list ever feels slow.

## Delivery phases

### Phase 1 — Reason flag on the inbox card

Derive the structural re-bubble reason in the inbox read model and surface it on each "New since last visit" card: "New", "Updated — N sections changed" (with section names where they fit), or "Comments added". No diff yet. Testable value on its own: the inbox stops being silent about *why* a doc came back, which is most of the day-to-day relief.

### Phase 2 — Section-aware diff view

The pure diff function plus the version accessor for the prior-at-baseline version, and a diff render mode on the doc page reachable by toggle, with mark-read on view. Testable value: open any eligible doc and see only the changed sections against what you last saw, and toggle back to the full render.

### Phase 3 — Default the card to the diff

Wire the two together: a content-change card with a prior version deep-links to the defaulted diff view; new and comment-only cards link to the full render. Testable value: the complete "click the flagged card, land on the delta" loop.

## Indicative implementation notes

Plan-level detail worth carrying forward — for `/feature-plan`, not constraints:

- **Version accessor.** `versions.py` already has `current_content`; add a sibling that returns the baseline version's `ParsedContent` (latest at-or-before a timestamp), reusing the same row→ParsedContent decode.
- **Read-state accessor.** `read_state.py` exposes `mark_read` / `mark_all_read` / `unread_document_ids` but no way to *read* a single doc's `last_read_at` — both reason derivation and the baseline lookup need it, so a small read accessor is new work.
- **Text extractor is new.** "Diff the rendered text" is not free — `render_section_doc` / `extract_safe_inner` return HTML `Markup`, not plain text, and there's no tag-stripping text extractor in `doc_render.py` today. A new extraction step is load-bearing; stdlib `difflib` over that extracted text is a candidate for the intra-section diff.
- **Pure diff module.** New module alongside `doc_content.py` / `doc_render.py`, no DB import, unit-testable in isolation. Match sections by `key`. The set of changed-section keys feeds both the diff render and the Phase-1 card label, so factor the "which sections changed" calc to be shared.
- **Reason derivation.** Reads the doc's events newer than the baseline and maps `event_type` to a reason class (`created`/`updated`/`reactivated` → content; `comment_*` → interaction). Note that synthesis submissions emit no event and feedback docs are excluded from the unread list — so the interaction reason in practice is comment events only.
- **Humanising section keys.** Reuse `ManifestSpec.section_labels` (key → label) from `doc_content.py`; fall back to a prettified key for unknown ones.
- **Opaque-body fallback.** Even though opaque docs don't reach the inbox re-bubble path today, the diff view should degrade gracefully (raw render, no diff) if reached for an opaque or version-less doc, rather than erroring on a missing prior.
- **Render mode plumbing.** The diff mode joins the existing `mode` branching in `doc_view.doc_shell` (native / raw-fallback / synthesis-native); the toggle is a small addition to `doc.html` + `doc.{css,js}`. The query-param-vs-sub-route choice for the diff URL is left to the plan (leaning query param), but Phase 3's card deep-link shape depends on it.
- **Testing focus.** The empty-textual-diff and `reactivated` paths are the trust-boundary edges — they're where tests must fail for the right reason, not the happy path.

## Design notes

- **Read-state trust boundary (round 1).** `last_read_at` is the single source of truth for "what I've seen" and pins both the unread predicate and the diff baseline — the one hard-to-reverse signal here. We keep mark-read firing on every view (including the diff), and rely on the empty-diff fallback so a formatting-only re-bubble never stamps you read while showing a blank delta. We rejected making mark-read conditional on a non-empty delta: extra state for little gain on a single-user tool.
- **A walk landing mid-request (round 1).** If the walker imports a new version between the version fetch and the read stamp, the baseline can advance past a version never shown, so it won't diff next time. Accepted as a low-probability edge on a single-user, timer-walked local app; the fix if it ever bites is to stamp the specific version seen rather than wall-clock time.
- **Read-side derivation over event payload (round 1).** Chose to recompute the reason/changed-sections in the read model rather than persist it on the version event — see Alternatives. Revisit only if the inbox list shows real cost.
