# flagged-inbox-diff

## Overview

Three increments, each one MR, over the existing structured-content stack — no migration, no new tables. **Phase 1** adds a new pure module `storage/doc_diff.py` that classifies the sections that changed between two document versions, and uses it in the inbox read model to flag *why* each "New since last visit" card re-surfaced ("New" / "Updated — N sections changed" / "Comments added"). **Phase 2** extends that module with an intra-section text diff, adds a `diff` render mode to the doc page reachable by a `?view=diff` toggle, and handles the formatting-only "empty diff" case by falling back to the full render with a note. **Phase 3** deep-links a content-change card straight to its defaulted diff view. The read baseline (`read_state.last_read_at`) anchors both the unread predicate and the diff's "prior" version; the existing per-view mark-read advances it.

## Key technical decisions

1. **New pure module `storage/doc_diff.py` — section classification first, intra-section diff second**
  Mirrors the existing DB-free pure modules (`doc_content.py`, `doc_render.py`): no DB import, fully unit-testable. Phase 1 introduces text extraction and section-level classification (the changed-section set the card label needs); Phase 2 adds the per-section text diff. Section bodies are opaque trusted HTML, so the diff works on *extracted text*, not the raw HTML fragment (a raw diff would surface markup noise). Match sections by their manifest `key`; a renamed key reads as one removed + one added (accepted).
  ```python
  # storage/doc_diff.py  (Phase 1 surface)
  from dataclasses import dataclass
  from typing import Literal
  from feature_skills_webapp.storage.doc_content import ParsedContent

  SectionStatus = Literal["added", "removed", "changed", "unchanged"]

  @dataclass(frozen=True)
  class SectionDiff:
      key: str
      status: SectionStatus
      # Phase 2 adds: segments for the intra-section ins/del rendering.

  @dataclass(frozen=True)
  class DocDiff:
      sections: tuple[SectionDiff, ...]   # in current order, removed appended

      @property
      def changed_keys(self) -> tuple[str, ...]:   # added | removed | changed
          ...
      @property
      def changed_count(self) -> int: ...
      @property
      def has_textual_change(self) -> bool:        # changed_count > 0

  def extract_text(html: str) -> str:
      """Tag-stripped, whitespace-collapsed text of a section body."""

  def diff_contents(prior: ParsedContent, current: ParsedContent) -> DocDiff:
      """Section-aware diff. A section is 'changed' when extract_text differs."""
  ```
2. **Baseline lookups: two small accessors, no new state**
  The "prior" version is the latest one created no later than the read baseline. We add a read accessor for the baseline timestamp (`read_state.py` currently only writes it) and a version accessor that decodes the baseline version's content, reusing the existing row→`ParsedContent` path in `current_content`. No per-document "last seen version" column.
  ```python
  # storage/read_state.py
  def last_read_at(conn: sqlite3.Connection, document_id: int) -> str | None:
      """The doc's last_read_at, or None if never read."""

  # storage/versions.py  (sibling of current_content)
  def content_at_or_before(
      conn: sqlite3.Connection, document_id: int, ts: str
  ) -> ParsedContent | None:
      """Latest version with created_at <= ts, decoded; None if no such version."""
  ```
3. **Reason derived read-side in `inbox.py`; `InboxCard` gains a structured reason + href**
  For each "New since last visit" card we classify the re-bubble from the doc's events newer than the baseline and (for a content change) the section diff, which needs the baseline-version lookup `content_at_or_before` — so that accessor lands in **Phase 1**, not Phase 2. Event mapping: `created`/`updated`/`reactivated` → content; `comment_submitted`/`comment_integrated` → interaction; `archived` can't reach this list (query is `status='active'`). A content re-bubble whose textual diff is empty (formatting-only re-save) is labelled "Updated (formatting only)" and does *not* get a diff link. The card carries a computed `href` so the template stays dumb (Phase 3 sets it to the diff view). Reason is attached only on the `new_since` path — `in_progress`/`recently_shipped`/`awaiting_input` cards are unchanged.
  ```python
  # storage/inbox.py
  @dataclass(frozen=True)
  class InboxReason:
      kind: Literal["new", "content", "comments"]
      label: str                       # humanised, e.g. "Updated — Technical approach, Vision +1 more"
      changed_count: int = 0
      has_diff: bool = False           # content & changed_count > 0

  @dataclass(frozen=True)
  class InboxCard:
      project: str
      feature: str | None
      label: str
      last_activity: str | None
      document_id: int | None = None
      badge: str = "context"
      reason: InboxReason | None = None      # new
      href: str | None = None                # new; None → template falls back to /doc/{id}

  def classify_reason(
      conn: sqlite3.Connection, document_id: int, doc_type: str, last_read: str | None
  ) -> InboxReason | None:
      """new vs content vs comments, with changed-section count/labels for content."""
  ```
4. **Diff is a new render mode of `/doc/{id}`, selected by `?view=diff`**
  Joins the existing `mode` branching in `doc_view.doc_shell` (`native` / `raw-fallback` / `synthesis-native`). A `?view=diff` query param requests the diff; it's honoured only for an eligible doc (section-shaped, a prior version exists, and the textual diff is non-empty). When requested but not eligible — first sighting, opaque, or formatting-only — we render `native` with a small "showing full document (no textual changes)" note rather than an empty pane. `mark_read` stays exactly where it is (fires for every mode), so the diff view advances the baseline for free. A toggle in the doc bar links between full and diff.
  ```python
  # web/doc_view.py — inside doc_shell's native branch, where `content` is bound
  # (guard requires content; only the native branch binds it — keep this code there,
  # don't reference `content` from the unavailable/synthesis branches).
  if request.query_params.get("view") == "diff" and content.shape == "sections":
      prior = content_at_or_before(conn, doc_id, last_read_at(conn, doc_id) or "")
      if prior is not None:
          doc_diff = diff_contents(prior, content)
          if doc_diff.has_textual_change:
              body_html = render_diff(doc_diff, manifest)   # doc_render.py
              mode = "diff"
          else:
              formatting_only = True   # template shows the "no textual changes" note
  # storage/doc_render.py
  def render_diff(doc_diff: DocDiff, manifest: ManifestSpec) -> Markup:
      """Changed sections with inline ins/del; unchanged sections collapsed/marked.
      Each text segment is markupsafe.escape()'d BEFORE wrapping — extract_text yields
      plain text that can contain literal < / & (e.g. from ), and the return
      is Markup (autoescape passthrough), so unescaped segments would render as live tags."""
      
  ```

## File structure

### New files

- `feature_skills_webapp/storage/doc_diff.py` — pure section/text diff (Phase 1 + 2).
- `feature_skills_webapp/storage/doc_diff_test.py` — unit tests for the pure module.

### Modified — Phase 1

- `storage/read_state.py` — add `last_read_at()` accessor.
- `storage/versions.py` — add `content_at_or_before()` (Phase 1 needs it for the prior-version lookup).
- `storage/inbox.py` — `InboxReason`, `classify_reason()`, `InboxCard.reason`, attach on `new_since_last_visit`.
- `web/templates/_inbox_body.html` — render the reason on "New since last visit" cards.
- `storage/inbox_test.py`, `storage/read_state_test.py`, `storage/versions_test.py` — reason + accessor tests.

### Modified — Phase 2

- `storage/doc_diff.py` — intra-section text diff (word-level `difflib` segments) for changed sections.
- `storage/doc_render.py` — add `render_diff()` (escapes each segment before wrapping in `<ins>`/`<del>`).
- `web/doc_view.py` — `?view=diff` → `diff` mode + formatting-only fallback flag.
- `web/templates/doc.html`, `web/static/doc.css`, `web/static/doc.js` — diff styling + full↔diff toggle in the doc bar.
- `storage/doc_diff_test.py`, `storage/doc_render_test.py`, `web/doc_view_test.py` — segments + render (incl. escaping) + mode + fallback tests.

### Modified — Phase 3

- `storage/inbox.py` — set `InboxCard.href` to `/doc/{id}?view=diff` when `reason.has_diff`.
- `web/templates/_inbox_body.html` — use `card.href` for the "New since last visit" feature link.
- `storage/inbox_test.py`, `web/routes_test.py` — href + end-to-end link tests.

## Phase 1 — Reason flag on the inbox card

### What's built

The pure section-classification half of `doc_diff.py` plus the read-model wiring that attaches a structured reason to every "New since last visit" card and renders it. No diff view yet — the value is the inbox no longer being silent about why a doc came back.

### Key logic

- `extract_text(html)`: an `HTMLParser` that collects character data (skipping tags) and **drops HTML comments**, collapsing all whitespace runs *including newlines*, so two section bodies differing only in markup/whitespace/comments compare equal (this is what makes the formatting-only gate hold).
- `content_at_or_before(conn, doc_id, ts)` (in `versions.py`, built this phase): the prior-version lookup `classify_reason` needs. With an empty baseline (`last_read_at` is `''` for a never-read doc) no real ISO timestamp is `≤ ''`, so it returns `None` → "New" — matching the first-sighting definition.
- `diff_contents(prior, current)`: build `{key: extract_text(body)}` for each side; a key in both with differing text is `changed`, only-current is `added`, only-prior is `removed`, equal is `unchanged`.
- `classify_reason(...)`: load `last_read`; gather the doc's events with `created_at > last_read`. If any is `created`/`updated`/`reactivated`, it's a content re-bubble: fetch current + prior content and run `diff_contents`. No prior version → `kind="new"`. `changed_count == 0` → label "Updated (formatting only)", `has_diff=False`. Otherwise `kind="content"`, `has_diff=True`, label naming up to two humanised section names then "+N more" (humanise via `ManifestSpec.section_labels`, fallback prettified key). If only comment events: `kind="comments"`, label "Comments added".

### Tests (`doc_diff_test.py`, `inbox_test.py`, `read_state_test.py`)

- `extract_text` strips tags and collapses whitespace; markup-only change → equal text.
- `diff_contents`: added / removed / changed / unchanged; reordered-only sections read as unchanged (order independent); two empty-`sections` contents (the "no <main>/zero sections" sentinel) diff to `changed_count == 0` without throwing.
- A changed plan `phase-2` section (a key in `repeated_prefixes`, not in `section_labels`) humanises via the prettified-key fallback to "Phase 2".
- Reason cases: new doc → "New"; content change → "Updated — N…" with correct N and names; formatting-only version → "Updated (formatting only)", `has_diff=False`; comment-only → "Comments added"; `reactivated` event → content; never-read-but-already-versioned → "New" (no version at-or-before the empty baseline).
- Label overflow: 3+ changed sections → "name, name +N more".
- `last_read_at()` returns the stamp, and `None` for an unread doc.
- `content_at_or_before()`: picks the right version at / before / between timestamps; `None` when the baseline (incl. the empty-string sentinel) predates all versions.

### MR chain

One MR titled `feat(flagged-inbox-diff): phase 1`.

## Phase 2 — Section-aware diff view

### What's built

The intra-section text diff, a diff render mode on the doc page reachable by `?view=diff` with a full↔diff toggle, and the formatting-only fallback. Independently reachable by URL before Phase 3 wires the card to it.

### Key logic

- `doc_diff.py`: for a `changed` section, compute inline segments via `difflib.SequenceMatcher` over the two extracted texts at **word granularity with `autojunk=False`** (char-level is unreadable; autojunk degrades on long sections), tagging runs `equal`/`insert`/`delete`; attach to `SectionDiff`. (`content_at_or_before` was built in Phase 1.)
- `doc_render.render_diff(doc_diff, manifest)`: section order from the manifest (as `render_section_doc`); changed sections render their segments — **each segment `markupsafe.escape()`'d before wrapping** in `<ins>`/`<del>` (the segment text is plain and may contain literal `<`/`&`); unchanged sections render collapsed or with an "unchanged" marker; added/removed labelled. Returns `Markup`.
- `doc_view.doc_shell`: honour `?view=diff` per Decision 4; when requested but not eligible, render `native` and pass a `formatting_only`/`no_diff` flag so the template shows the note. Pass a `view` value to the template for the toggle's active state.
- `doc.html`/`doc.css`/`doc.js`: a full↔diff control in the doc bar (a link toggling `?view=`); `ins`/`del` styling; the formatting-only note. The toggle only shows for eligible section docs.

### Tests (`doc_diff_test.py`, `versions_test.py`, `doc_render_test.py`, `doc_view_test.py`)

- Intra-section segments: a changed body yields the expected equal/insert/delete runs; an unchanged body yields all-equal.
- `render_diff`: changed section shows ins/del markup; added/removed labelled; unchanged marked; output is `Markup` (autoescape passthrough).
- **Escaping**: a section whose text contains `<script>` (e.g. from a code sample) renders as escaped text inside the diff, not a live tag.
- doc_view: `?view=diff` on an eligible doc → diff mode; on a first-sighting/opaque doc → native + note (never an empty diff); the formatting-only version → native + note; mark-read still stamped on the diff view.

### MR chain

One MR titled `feat(flagged-inbox-diff): phase 2`.

## Phase 3 — Default the card to the diff

### What's built

The end-to-end loop: a content-change card with a non-empty diff links straight to `/doc/{id}?view=diff`; new, comment-only, and formatting-only cards link to the plain doc.

### Key logic

- `inbox.py`: when building a `new_since` card, set `href = f"/doc/{document_id}?view=diff"` if `reason.has_diff` else `f"/doc/{document_id}"`.
- `_inbox_body.html`: the "New since last visit" feature link uses `card.href` (fallback `/doc/{card.document_id}` when `href` is None, preserving current behaviour for the other categories).

### Tests (`inbox_test.py`, `routes_test.py`)

- Card href: content-with-diff → `?view=diff`; new / comments / formatting-only → plain `/doc/{id}`.
- Route-level: the rendered inbox links a content-change card to the diff view, and following it lands on diff mode (200, diff markup present).

### MR chain

One MR titled `feat(flagged-inbox-diff): phase 3`.

## QC

Before each commit, run the full gate from `CLAUDE.md` § "QA / quality control": `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, `uv run pytest` — all must pass. No dependency changes are expected; if any `pyproject.toml` change sneaks in, follow the reinstall-and-restart note in `CLAUDE.md` for the running service. After merge, restart the service to pick up code changes (`systemctl --user restart feature-skills-webapp`).

## Checklist

### Phase 1: Reason flag

- Create `storage/doc_diff.py` with `extract_text` (drops comments, collapses all whitespace incl. newlines), `SectionDiff`/`DocDiff`, and `diff_contents` (section-level classification only).
- Add `last_read_at()` accessor to `storage/read_state.py`.
- Add `content_at_or_before()` to `storage/versions.py` (empty-string baseline → None).
- Add `InboxReason`, `classify_reason()`, and `InboxCard.reason` to `storage/inbox.py`; attach the reason on the `new_since_last_visit` path.
- Render the reason on "New since last visit" cards in `_inbox_body.html`.
- Tests: `doc_diff_test.py` (extract/classify, reordered, empty-sentinel, phase-* fallback), reason cases + label overflow in `inbox_test.py`, `last_read_at` in `read_state_test.py`, `content_at_or_before` in `versions_test.py`.
- Run QC; commit and open MR `feat(flagged-inbox-diff): phase 1`.

### Phase 2: Diff view

- Extend `doc_diff.py` with intra-section `difflib` segments (word-level, `autojunk=False`) on changed sections.
- Add `render_diff()` to `storage/doc_render.py`, escaping each segment before wrapping in `<ins>`/`<del>`.
- Add the `?view=diff` branch + formatting-only fallback to `doc_view.doc_shell` (keep the guard inside the native branch where `content` is bound).
- Add diff styling, the formatting-only note, and the full↔diff toggle to `doc.html`/`doc.css`/`doc.js`.
- Tests: segments (`doc_diff_test.py`), `render_diff` incl. escaping (`doc_render_test.py`), mode + fallback + mark-read (`doc_view_test.py`).
- Run QC; commit and open MR `feat(flagged-inbox-diff): phase 2`.

### Phase 3: Deep-link

- Set `InboxCard.href` in `inbox.py` (diff view when `reason.has_diff`, else plain doc).
- Use `card.href` for the "New since last visit" feature link in `_inbox_body.html` (fallback to `/doc/{id}`).
- Tests: href cases (`inbox_test.py`) + route-level deep-link to diff (`routes_test.py`).
- Run QC; commit and open MR `feat(flagged-inbox-diff): phase 3`.
