# writable-doc-types — Plan

## Overview

The webapp refuses to *write* any document whose type isn't one of four built-ins (`context`, `requirements`, `plan`, `*-feedback`). Everything else in the stack already handles arbitrary types — a document of any type renders, diffs, and exports today; the write gate is the wall. This plan takes the wall down for bespoke, feature-scoped types, extends inline commenting so those docs can actually be reviewed, and gives unknown types a defined badge and a place in the feature listing instead of silently vanishing.

It lands in three separately-shippable MRs: (1) open the write boundary and signal fat-fingered types; (2) make bespoke docs first-class in the reviewing UI — comments, badge, feature-page listing; (3) migrate the north-star documents that motivated the work onto their natural types.

## Key decisions

1. **Reserved-name denylist, not an allowlist**
  The gate flips from "admit only known types" to "admit anything feature-scoped except reserved names". Reserved = `features` (project tracker) and `review` (holds a UI rank/label but no manifest). The `-feedback` suffix stays admitted with its existing free-instance semantics. The invariants (feature required, instance 1 for non-feedback, 1 MB cap, full-replacement) are unchanged.
  ```
  RESERVED_TYPES = frozenset({"features", "review"})

  def validate_writable(doc_type: str, feature: str | None, instance: int) -> None:
      if doc_type in RESERVED_TYPES:
          raise SubmitError(f"doc_type {doc_type!r} is reserved and not writable")
      if feature is None:
          raise SubmitError("feature must be specified (project-level docs are not writable)")
      if instance != 1 and not doc_type.endswith("-feedback"):
          raise SubmitError(f"instance must be 1 for {doc_type!r} (got {instance})")
  ```
  `WRITABLE_SECTION_TYPES` is no longer the gate; grep confirms it's only referenced by this function, so remove it.
2. **Commenting is a one-predicate change — opaque docs already render "native"**
  A non-feedback opaque doc already renders in `mode == "native"` (`doc_view.py:146-148`), and the comment affordance is gated only on `mode == "native" and is_commentable` (`doc_view.py:187`). So no comment-layer wiring is needed — widening the `is_commentable` predicate to "any active, feature-scoped, non-feedback doc" is sufficient.
  ```
  is_commentable = (
      row["status"] == "active"
      and row["feature"] is not None
      and not row["type"].endswith("-feedback")
  )
  ```
  Feedback docs are doubly excluded — by the suffix test and by never reaching "native" mode. **Blast radius (acknowledged, intended):** this turns on inline comments for *every existing `context` doc in every project*, not just new bespoke docs — a fleet-wide behaviour change on stored data. Confirmed desirable: commenting on context docs is a plus.
3. **Unknown-type signal: a log line, not a new event**
  To make a fat-fingered type (`requirement` → a silent orphan) discoverable without touching event rendering, `submit_document` logs a warning when it *creates* a doc of a type outside the known set (`{context, requirements, plan, features, review}` and non-`-feedback`). Non-blocking: it never affects the write result. A surfaced inbox event is deferred — and it is belt-and-braces anyway, because Phase 2's feature-page listing already makes a mistyped orphan visible.
4. **Default badge for unknown types**
  `badge_kind` returns the raw type for unknowns, yielding an undefined `.badge-<type>` class (falls back to the neutral `.doc-badge` base). Map unknowns to a defined `"doc"` kind and add a `.badge-doc` rule so the treatment is intentional, in both templates that render doc badges (`feature.html` and the inbox `index.html`). While there, add the missing `.badge-context` rule too (context badges are currently unstyled).
5. **Feature-page inclusion**
  The overview page filters its primary list to `type in DOC_TYPE_ORDER` (`feature_page.py:38`), dropping bespoke docs from all three lists. Change the predicate to "active, non-`-feedback`" so bespoke docs join the primary list; `doc_type_rank` already sorts unknowns last, so ordering needs no change.

## Data model

No schema change. `documents.type` is free-text and the versioned store already persists the opaque whole-body shape bespoke docs use.

The only new named concept is the reserved-type set (`features`, `review`) enforced at the write boundary. The unknown-type log signal adds no persisted state.

## Contract

No change to the HTTP request/response shape of `PUT /api/documents/{project}/{feature}/{doc_type}/{instance}`. The only behavioural change is which `doc_type` values are accepted: previously the four built-ins; now any feature-scoped type except the reserved names.

- `PUT …/vision/1` with `{"body": "<section>…</section>", "actor": "agent"}` → 200 (opaque doc, as for feedback docs).
- `PUT …/features/1` or `…/review/1` → 400 "reserved and not writable".
- `PUT …/vision/2` → 400 "instance must be 1".
- `PUT` at a project-level path (feature = `-`) for a bespoke type → 400 "feature must be specified".

## File structure

### Phase 1 — write boundary

- `feature_skills_webapp/storage/documents.py` — new `RESERVED_TYPES`; rewrite `validate_writable`; remove `WRITABLE_SECTION_TYPES`; add the unknown-type log in `submit_document`'s create branch.
- `feature_skills_webapp/storage/documents_test.py` — see Phase 1 tests below.

### Phase 2 — first-class UI

- `feature_skills_webapp/web/doc_view.py` — widen the `is_commentable` predicate.
- `feature_skills_webapp/web/doc_view_test.py` — commentable tests (name them with "commentable"); comment round-trip on a bespoke doc; sibling-nav confirmation.
- `feature_skills_webapp/storage/inbox.py` — `badge_kind` default → `"doc"` for unknowns.
- `feature_skills_webapp/storage/inbox_test.py` — badge default assertion.
- `feature_skills_webapp/web/feature_page.py` — primary-list predicate includes bespoke types.
- `feature_skills_webapp/web/feature_page_test.py` — a bespoke doc appears in the primary list, ranked last.
- `feature_skills_webapp/web/templates/feature.html` and `web/templates/index.html` — add `.badge-doc` and `.badge-context` rules.
- *Best-effort:* `web/static/doc.js` / `web/static/doc.css` / `templates/doc.html` — comments-rail scroll behaviour (see Phase 2).

### Phase 3 — content migration (no code in this repo)

- API writes against the `ai-eng-planning` project: PUT the two north-star docs under `vision` / `system-map`, drop the vehicle notes, then **archive** (not delete) the old `requirements`-typed instances.

## Verification

### Quality control (all phases)

Run the full QC battery from `CLAUDE.md` — all must pass:

```
uv run ruff format --check .
uv run ruff check .
uv run ty check .
uv run pytest
```

### Phase 1 — boundary

```
# Boundary battery + the round-trip test:
uv run pytest feature_skills_webapp/storage/documents_test.py -k "writable or roundtrip" -q
```

Live smoke against a throwaway feature (attempt-first; clean up after). Pass URLs unquoted so the localhost guard permits them. The `POST /api/projects/{p}` and `.../features/{f}` routes are confirmed to exist (`app.py:130,146`):

```
curl -fsS -X POST http://127.0.0.1:8800/api/projects/scratch
curl -fsS -X POST http://127.0.0.1:8800/api/projects/scratch/features/wdt-smoke
# Accept a bespoke type:
curl -fsS -X PUT http://127.0.0.1:8800/api/documents/scratch/wdt-smoke/vision/1 \
  -H 'Content-Type: application/json' \
  -d '{"body": "<section id=\"s\"><p>hi</p></section>", "actor": "agent"}'   # → 200
# Reject reserved + bad instance (expect HTTP 400):
curl -s -o /dev/null -w '%{http_code}\n' -X PUT http://127.0.0.1:8800/api/documents/scratch/wdt-smoke/review/1 -H 'Content-Type: application/json' -d '{"body":"x"}'   # 400
curl -s -o /dev/null -w '%{http_code}\n' -X PUT http://127.0.0.1:8800/api/documents/scratch/wdt-smoke/vision/2 -H 'Content-Type: application/json' -d '{"body":"x"}'   # 400
```

### Phase 2 — reviewing UI

```
uv run pytest feature_skills_webapp/web/doc_view_test.py -k "comment or sibling" -q
uv run pytest feature_skills_webapp/web/feature_page_test.py -q
uv run pytest feature_skills_webapp/storage/inbox_test.py -k badge -q
```

Live (restart the service first — `systemctl --user restart feature-skills-webapp` — so it reflects the code):

```
# Comment UI present on the bespoke doc:
curl -fsS http://127.0.0.1:8800/doc/<bespoke-doc-id> | grep -c '__commentable = true'   # ≥1
# Feedback doc still NOT commentable:
curl -fsS http://127.0.0.1:8800/doc/<feedback-doc-id> | grep -c '__commentable = false'  # ≥1
# Bespoke doc appears on the feature page (label renders capitalised "Vision"):
curl -fsS http://127.0.0.1:8800/feature/scratch/wdt-smoke | grep -ic vision   # ≥1
```

### Phase 3

GET each migrated doc under its new type and assert 200 with no "riding as requirements" vehicle text; confirm the old `requirements`-typed instance is archived. (Runs against `ai-eng-planning`; attempt-first.)

## Qc

Follow `CLAUDE.md` at implementation time — currently: `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, `uv run pytest`, all green before each MR. After code changes, restart the systemd service to see them live; no dependency changes are expected.

**Testing discipline (per `TESTING.md`) — read carefully:** Not every boundary test can go red on the parent. `features` and `review` are *already* rejected on the parent (they're not in `WRITABLE_SECTION_TYPES`), and the new message still contains "not writable", so `rejects_features` (which already exists) and the renamed `rejects_reserved_review` are **regression guards, not pins** — they pass with and without the change. The tests that genuinely flip red-on-parent are *accept-a-bespoke-type*, *reject-bespoke-instance-2*, and *reject-bespoke-feature-none*. Trap: on the parent a bespoke type raises "not writable" first, so those last two MUST assert the **specific new messages** (`match="instance must be 1"`, `match="feature must be specified"`) — a bare `pytest.raises(SubmitError)` or `match="not writable"` would pass on the parent and pin nothing. Confirm each of the three is red on the parent commit.

## Checklist

### Phase 1: Open the write boundary

- Add `RESERVED_TYPES = {"features", "review"}` and rewrite `validate_writable` to the denylist; remove `WRITABLE_SECTION_TYPES`.
- Log a non-blocking warning in `submit_document`'s create branch when the type is outside the known set.
- Rename `rejects_unknown_type` → `rejects_reserved_review`; keep existing `rejects_features` (regression guards).
- Add red-on-parent pins: accept-bespoke; reject-bespoke-instance-2 (`match="instance must be 1"`); reject-bespoke-feature-none (`match="feature must be specified"`). Confirm each red on parent.
- Add write→read round-trip test for a bespoke opaque body; still-accept `-feedback` at instance>1; assert unknown-type log fires.
- Run QC (ruff format/check, ty, pytest); open MR 1.

### Phase 2: First-class in the reviewing UI

- Widen `is_commentable` to active + feature-scoped + not `-feedback` in `doc_view.py`.
- Default `badge_kind` unknown types to `"doc"`; add `.badge-doc` + `.badge-context` in `feature.html` and `index.html`.
- Include bespoke (non-feedback) types in the `feature_page.py` primary-list predicate.
- Tests: bespoke & context commentable (name them `*commentable*`), feedback not, comment round-trip; sibling-nav includes bespoke ranked last; badge default; bespoke doc listed on feature page.
- Best-effort: investigate the comments-rail scroll behaviour; apply the simpler fix (likely a sticky rail) or note-and-leave. Must not block the MR.
- Run QC; restart service; run the live smoke assertions; open MR 2.

### Phase 3: Migrate the motivating documents

- PUT north-star docs under `vision` / `system-map` in `ai-eng-planning`, vehicle notes removed.
- Archive (not delete) the old `requirements`-typed instances.
- Verify each migrated doc GETs 200 under its new type with no vehicle text.

## Phase 1

Rewrite `validate_writable` to the reserved-name denylist, remove the dead `WRITABLE_SECTION_TYPES`, and log a non-blocking warning when `submit_document` creates a doc of an unknown type. After this, a bespoke doc is writable and — via the existing opaque path — renders at `/doc/N` and diffs. One MR.

**Tests:**

- *Accept* a bespoke type at instance 1 (flips red on parent — assert no raise).
- *Reject* a bespoke type at instance 2 with `match="instance must be 1"`, and at feature=None with `match="feature must be specified"` (both flip red on parent; the specific match is what makes them pins — see QC).
- Rename existing `test_validate_writable_rejects_unknown_type` (uses `review`) → `rejects_reserved_review`; keep the existing `rejects_features` (regression guards, not new).
- Still *accept* a `-feedback` name at instance > 1.
- **Write→read round-trip** (requirements-mandated): PUT a bespoke opaque body, read it back via `current_content`, assert the body round-trips unchanged.
- Assert the unknown-type log fires on creating a bespoke type (and not for a known type).

## Phase 2

Widen `is_commentable` so any active, feature-scoped, non-feedback doc is commentable (opaque docs already render native, so no further wiring). Default the badge kind for unknown types to `"doc"` and add `.badge-doc` + `.badge-context` rules in both doc-badge templates. Include bespoke (non-feedback) types in the feature-overview primary list. One MR.

**Tests:** a bespoke and a context doc are commentable; a feedback doc is not; a comment round-trips on a bespoke doc; sibling-nav includes a bespoke doc ranked last (confirmation the existing `siblings()` needs no change); unknown type → `"doc"` badge kind; a bespoke doc appears in the feature primary list.

**Best-effort — comments-rail scroll.** The right-margin comments rail doesn't track the page as you scroll: only its `<h3>` is sticky (`doc.css:342-352`); the comment list (`#rail-list`, rendered in `doc.js` ~`renderRail`) is plain-flow, so on a long doc the comments stay pinned at the top and you scroll away from them. Intended behaviour is undecided — the two reasonable options are (a) make the rail/list sticky so it stays in view, or (b) anchor each comment near its excerpt (Google-Docs style). Take a best-effort look and pick the simpler win (likely a sticky rail); if it turns into real work, note it and leave it — this must not block the phase or the MR.

## Phase 3

In the `ai-eng-planning` project, PUT the two north-star documents under their natural types (`vision`, `system-map`) with the "riding as requirements" vehicle notes removed, then **archive** (not delete) the old `requirements`-typed instances. Content migration over the API — no code change in this repo. Gated on Phases 1–2 being merged *and* smoke-verified. One MR (or content change, per that project's workflow).
