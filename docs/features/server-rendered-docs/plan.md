# server-rendered-docs

## Overview

Replace the iframe-based doc view with server-side rendering from the structured content F1 stores. `doc_shell` stops embedding `<iframe src="/doc/{id}/raw">` and instead renders the stored content directly into a webapp-owned page, applying one webapp-owned presentation layer (CSS/JS as static assets) to every doc. Comments (requirements/plan) and synthesis (feedback) become native widgets posting to the existing, unchanged doc-id-keyed endpoints, and prefill from already-submitted state. A single render-safe extractor strips document furniture (`<head>`/`<style>`/`<script>`) so author HTML can't leak styles or execute scripts in the webapp origin. The corpus is self-authored on localhost, so the trust model is **accident-prevention** (stop a doc's styles/scripts bleeding into the shell), not hostile-author hardening — stripping script/style/head is sufficient; no sanitiser dependency is pulled in. Ships in two MRs: Phase 1 (render engine + presentation + native comments, feedback still framed transitionally); Phase 2 (native synthesis, iframe fully retired). No new tables, no migration.

## Key technical decisions

1. **A pure render module separate from the importer's parser**
  F1's `_SectionParser` in `doc_content.py` is purpose-built for the import side (section extraction for versioning) and stays untouched. A new pure, DB-free module `storage/doc_render.py` holds the render-time logic so it's unit-testable in isolation and reusable by the raw hatch and the future agent write path. Both functions return `markupsafe.Markup` so the trusted, sanitised HTML crosses Jinja's autoescape boundary intact (see Decision 4). Parsing uses the stdlib `html.parser` (as `_SectionParser` already does) — **no new dependency**. Rendering keys off the doc's *stored* sections; the manifest supplies order + labels only (it's an aspirational superset, not 1:1 with any one doc).
  ```python
  def render_section_doc(content: ParsedContent, manifest: ManifestSpec) -> Markup:
      """Inner HTML for <main>: each STORED section body wrapped in
      <section id="{key}">, ordered by the manifest's section_labels; keys not in
      the manifest rendered last in stored order. Section bodies are already inner
      content (carry their own <h2>). Returns markupsafe.Markup."""

  def extract_safe_inner(html: str) -> Markup:
      """For opaque docs (tracker): return the inner HTML of <main class="document">
      if present, else <body>, with <script>/<style>/<head> removed.
      The single render-safe chokepoint for whole-file stored bodies. Threat model is
      accident-prevention (trusted self-authored corpus), so inline event-handler
      attributes / javascript: URLs are NOT scrubbed — documented, not an oversight.
      Returns markupsafe.Markup."""
  ```
2. **Manifest gains ordered (key, label) for rendering and TOC**
  Per the requirements, `ManifestSpec` is extended with an ordered `section_labels` tuple — the single source of truth for section order and heading/TOC labels. `expected_keys` today is *not* an import gate (`parse_content` branches on `shape` only; the walker doesn't validate keys) — it's consumed only by tests. So turning it into a property derived from `section_labels` is safe. One gotcha: making it a property removes it from `__init__`, so the `_MANIFESTS` literals (context/requirements/plan) **must** be rewritten to pass `section_labels`, or construction raises `TypeError` (no production caller passes `expected_keys=`; grep-confirmed).
  ```python
  @dataclass(frozen=True)
  class ManifestSpec:
      shape: Literal["sections", "opaque"]
      section_labels: tuple[tuple[str, str], ...] = ()   # ordered (key, label)
      repeated_prefixes: tuple[str, ...] = ()

      @property
      def expected_keys(self) -> tuple[str, ...]:
          return tuple(k for k, _ in self.section_labels)
  ```
3. **Presentation relocates to tested static assets**
  The doc chrome moves into `web/static/doc.css` and `web/static/doc.js` (served from the existing `/static` mount), owned once instead of copied per template. `doc.js` holds the comment rail, click-to-comment, TOC scroll-spy, prefill hydration, and (Phase 2) the synthesis submit logic. "Tested" here means: the JS is a single served asset referenced by the page, and its *behaviour contract* (the POST payloads it produces) is covered by Python integration tests that exercise the endpoints, plus a test asserting the rendered page references the assets and emits the expected widget DOM. No JS unit-test harness is introduced. Highlight.js stays a CDN `<script>` in `doc.html` (consistent with today's docs). The `doc.css`/`doc.js` links carry a version query-string (e.g. `?v=<mtime>`) so a service restart doesn't serve a stale cached asset.
4. **doc_shell renders natively; doc_raw demoted to the hatch**
  `doc_shell` reads `current_content()` and renders inline; `doc_raw` stays only behind the "View source" link. When `current_content()` is `None` (unparsed / pre-F1), `doc_shell` falls back to the framed raw render with a visible note (reading the same source as the `/raw` hatch for consistency). Feedback uses a transitional framed branch in Phase 1, removed in Phase 2. Consequence: during Phase 1, `doc.html` carries **two layouts** — the new native page and the legacy iframe+scraping shell for feedback/fallback — gated on `mode`; scope the template work accordingly. The native render is emitted via Jinja `{{ body_html }}` where `body_html` is the `Markup` from Decision 1 (autoescape is on, so unmarked strings would render as visible tags).
  ```python
  # doc_shell, after fetching row:
  content = current_content(conn, doc_id)
  if content is None:
      mode = "raw-fallback"            # iframe /raw + note; nothing regresses
  elif is_feedback and not PHASE_2:
      mode = "framed"                  # transitional; removed in Phase 2
  elif content.shape == "opaque":
      body_html = extract_safe_inner(content.sections[0].body)   # tracker
      mode = "native"
  else:
      body_html = render_section_doc(content, manifest_for(row["type"]))
      mode = "native"
  ```
5. **Prefill is a doc-id read at render time**
  The shell already holds `document_id`, so prefill reads active `comments` and `synthesis_responses` directly by id — sidestepping the `source_path`-keyed GET accessors. The data is emitted into the page as JSON and hydrated by `doc.js`.
  ```python
  comments_prefill = conn.execute(
      "SELECT id, excerpt, text FROM comments "
      "WHERE document_id = ? AND status = 'active' ORDER BY id", (doc_id,),
  ).fetchall()
  synthesis_prefill = conn.execute(
      "SELECT item_num, response, routine_flag FROM synthesis_responses "
      "WHERE document_id = ?", (doc_id,),
  ).fetchall()
  ```
6. **Native synthesis: parse the opaque feedback body into items (Phase 2)**
  Feedback is stored opaque (whole file). `parse_feedback_items()` extracts the per-item structure and the webapp renders its own widgets, posting the existing `{responses, routine_flags}` shape. The whole-file body is never inlined.
  ```python
  @dataclass(frozen=True)
  class FeedbackItem:
      item_num: int
      tier: str          # "needs-input" | "feedback" | "routine"
      title_html: str    # inner HTML of the item's <h3> / routine body
      detail_html: str   # inner HTML of .detail (empty for routine)
      my_take_html: str  # inner HTML of .my-take (empty for routine)
      kind: str          # "response" (textarea) | "routine" (flag button)

  def parse_feedback_items(html: str) -> list[FeedbackItem]:
      """Parse a stored feedback doc into ordered items across the three tiers,
      keyed by data-item; furniture (head/style/script) discarded."""
  ```

## File structure

### New files

- `feature_skills_webapp/storage/doc_render.py` — pure render module: `render_section_doc`, `extract_safe_inner` (Phase 1); `parse_feedback_items` + `FeedbackItem` (Phase 2).
- `feature_skills_webapp/storage/doc_render_test.py` — unit tests for the render module.
- `feature_skills_webapp/web/static/doc.css` — relocated doc presentation (chrome, TOC, rail, synthesis widgets).
- `feature_skills_webapp/web/static/doc.js` — comment rail, click-to-comment, scroll-spy, prefill hydration (Phase 1); synthesis submit (Phase 2).

### Modified files

- `feature_skills_webapp/storage/doc_content.py` — extend `ManifestSpec` with `section_labels`; populate per type; `expected_keys` becomes derived.
- `feature_skills_webapp/web/doc_view.py` — `doc_shell` renders natively + prefill reads + fallback; `doc_raw` unchanged as the hatch.
- `feature_skills_webapp/web/templates/doc.html` — full server-rendered page (TOC, main, rendered content, comment rail, footer) linking the static assets; Phase 1 keeps a framed branch for feedback, removed in Phase 2.
- `feature_skills_webapp/web/doc_view_test.py` — native render / prefill / fallback / states / synthesis tests.
- `feature_skills_webapp/storage/doc_content_test.py` — manifest `section_labels` tests.

## Phase 1 — Native render engine + presentation + native comments

### What's built

Server-render context, requirements, plan and the tracker doc into the webapp-owned page; drop the iframe for these (feedback keeps a transitional framed branch). Manifest-driven ordering, TOC scroll-spy, syntax highlighting, breadcrumbs, sibling nav, and the missing/archived/503 states all preserved. Native comment rail for requirements/plan, prefilled from existing active comments, posting to the unchanged endpoint. Raw-render fallback when a doc has no stored version. Presentation lands as `static/doc.css` + `static/doc.js`; widgets are keyboard-operable and labelled.

### Files touched

`doc_render.py` (new: `render_section_doc`, `extract_safe_inner`), `doc_content.py` (manifest labels), `doc_view.py` (`doc_shell`), `templates/doc.html`, `static/doc.css`, `static/doc.js`, plus the test files.

### Tests

- Render module: section ordering by manifest, unknown-key fallthrough, `extract_safe_inner` strips `<script>`/`<style>` and returns inner content.
- Manifest: `section_labels` present/ordered per type; `expected_keys` derives correctly.
- doc_shell: context/requirements/plan render natively (content present, *no* `<iframe>`, and a stored body's `<h2>` renders as a real tag — *not* escaped — proving the `Markup` path); tracker renders via the opaque extractor; fallback-to-raw when no version row; 404 / missing / archived / 503 states intact; page references `/static/doc.js` + `/static/doc.css`.
- Rewrite (not just augment) the existing iframe-asserting tests — e.g. `test_doc_shell_200_with_breadcrumbs_and_iframe` and the `doc_raw`-as-render-path assumptions — since non-feedback docs no longer iframe.
- Comments: rail prefilled from existing active comments; submit still hits `POST /doc/{id}/comments` with the same payload (endpoint test unchanged).

### MR chain

One MR titled `feat(server-rendered-docs): phase 1`.

## Phase 2 — Native synthesis for feedback docs

### What's built

Render feedback docs natively: `parse_feedback_items()` extracts the item structure from the opaque body, the template renders webapp-owned per-item widgets (response textareas keyed by `item_num`, routine flag controls), prefilled from existing `synthesis_responses`. `doc.js` gains the submit handler building `{responses, routine_flags}` for the unchanged endpoint. The transitional framed branch and `doc.html`'s inline scraping scripts are removed; `doc_raw` survives only as the `/raw` hatch. Whole corpus is now iframe-free.

### Files touched

`doc_render.py` (new: `parse_feedback_items`, `FeedbackItem`), `doc_view.py` (remove feedback framed branch), `templates/doc.html` (synthesis widgets; drop scraping scripts), `static/doc.js` (synthesis submit + prefill), plus tests.

### Tests

- Render module: `parse_feedback_items` across all three tiers, item ordering, routine vs response kinds, malformed/empty body.
- doc_shell: feedback renders native widgets (no `<iframe>`); response/flag widgets prefilled from existing rows.
- Synthesis: submit integration produces the same `{responses, routine_flags}` shape (endpoint test unchanged); a guard test asserting no doc *with stored content* emits an `<iframe>` in the rendered shell (the no-version raw fallback legitimately still frames, so the guard is scoped to versioned docs).

### MR chain

One MR titled `feat(server-rendered-docs): phase 2`. Must follow Phase 1 within the same branch/work cycle so no iframe+native halfway house persists.

## QC

Run the full quality gate from `CLAUDE.md` § "QA / quality control" before each commit — all must pass: `uv run ruff format .`, `uv run ruff check .`, `uv run ty check .`, `uv run pytest`. Per `CLAUDE.md` § "Running the deployed service", restart the systemd user service after code changes (`systemctl --user restart feature-skills-webapp`) to see edits live; no dependency changes are expected here, so no reinstall.

## Checklist

### Phase 1: Render + presentation + comments

- Extend `ManifestSpec` with ordered `section_labels`; rewrite the `_MANIFESTS` literals (context/requirements/plan) to pass it; make `expected_keys` a derived property; update `doc_content_test.py`.
- Create `storage/doc_render.py` with `render_section_doc` and `extract_safe_inner` (both return `markupsafe.Markup`, stdlib `html.parser`); add `doc_render_test.py` (ordering, unknown keys, script/style/head stripping, Markup type).
- Add `web/static/doc.css` and `web/static/doc.js` (comment rail, click-to-comment, TOC scroll-spy, prefill hydration), linked with a `?v=<mtime>` cache-buster; ensure widgets are keyboard-operable and labelled.
- Rewrite `templates/doc.html` as a full server-rendered page (TOC, main, rendered content, comment rail, footer) linking the static assets; keep a transitional framed branch for feedback only.
- Update `doc_shell` to render natively from `current_content()` for context/requirements/plan/tracker, with raw-render fallback when no version; preserve breadcrumbs, sibling nav, and missing/archived/503 states.
- Wire native comments for requirements/plan: prefill the rail from active comments (doc-id read) and submit to the existing `POST /doc/{id}/comments`.
- Update `doc_view_test.py`: rewrite the existing iframe-asserting tests, and add native render per type (no iframe; stored `<h2>` renders un-escaped), tracker render, fallback-to-raw, states, comment prefill, static-asset reference.
- Run full QC (ruff format/check, ty, pytest); restart the service; open a doc in the inbox to confirm; commit + MR `feat(server-rendered-docs): phase 1`.

### Phase 2: Native synthesis

- Add `parse_feedback_items` + `FeedbackItem` to `doc_render.py`; unit tests across all tiers, ordering, routine vs response, malformed/empty body.
- Render native synthesis widgets in `doc.html` from parsed items (per-item response textareas keyed by `item_num`, routine flag controls), prefilled from existing responses.
- Add synthesis submit + prefill logic to `static/doc.js` (build `{responses, routine_flags}`, post to the unchanged endpoint).
- Switch `doc_shell` feedback path to native; remove the transitional framed branch and `doc.html`'s inline scraping scripts; keep `doc_raw` only as the `/raw` hatch.
- Add tests: feedback native render + widget prefill, synthesis submit integration, and a guard asserting no doc *with stored content* emits an `<iframe>` (raw fallback exempted).
- Run full QC; restart the service; confirm a feedback doc in the inbox; commit + MR `feat(server-rendered-docs): phase 2`.
