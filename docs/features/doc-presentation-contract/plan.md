# doc-presentation-contract — Plan

## Overview

Make the webapp render skill-authored document HTML correctly without silent CSS drift, in three independently-shippable phases. **Phase 1** widens and documents the canonical `web/static/doc.css` to cover the vocabulary the corpus actually emits (tables, `h4`, `blockquote`, `hr`, definition lists, and the semantic classes) and adds a lightweight test that fails if a covered selector disappears — this alone fixes the 147 section-docs and the reported bug. **Phase 2** serves that commented stylesheet to skills as the discoverable contract at a stable URL, pointed to from the existing manifest endpoint, and advertises the `extra_css` affordance. **Phase 3** adds the scoped `extra_css` escape hatch: a top-level field on the document write, stored in the content version, rendered scoped to the document body in native mode only, with opaque docs' own `<style>` scope-and-kept at render time, and each non-empty use logged as an event for the retro ratchet.

## Key decisions

### 1. `extra_css` lives on `ParsedContent` and in `serialise()`

It must version atomically with the body it styles and drive the existing change-detection (`serialise(cur) != serialise(content)` in `submit_document`). So it is a field on the content object, not a separate column.

```
@dataclass(frozen=True)
class ParsedContent:
    shape: Literal["sections", "opaque"]
    sections: tuple[Section, ...]
    extra_css: str = ""   # NEW — default "" so existing callers/rows are unaffected
```

`serialise()` emits the `"extra_css"` key **only when non-empty**, so the stored `content_json` of every existing doc stays byte-identical and no spurious version is cut on the next save:

```
payload = {"shape": content.shape,
           "sections": [{"key": s.key, "body": s.body} for s in content.sections]}
if content.extra_css:
    payload["extra_css"] = content.extra_css
return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
```

Both decoders (`current_content`, `content_at_or_before` in `versions.py`) read `extra_css=data.get("extra_css", "")`.

### 2. `extra_css` is a top-level field on the document write

Confirmed in requirements review (R1). It is a sibling to `sections`/`body` in the PUT JSON, not nested in the sections payload. `build_content` gains an `extra_css: str | None` parameter; an absent / empty / whitespace-only value normalises to `""` (nothing stored, no event). Size-bounded by the existing `MAX_BODY_BYTES` (1 MB).

### 3. Scoping mechanism: `@scope (#doc-main)` + source order, native render only

Respected CSS is emitted inside `<style>@scope (#doc-main) { … }</style>`, which confines it to the document body and cannot reach the shell chrome (breadcrumbs, comment rail, action bar) — the property the original F2 strip protected. The style is emitted *after* the canonical `doc.css` link.

**Precedence model (R2):** additive, not replacement. The canonical base rules still apply per-property; extra_css adjusts on top. At equal specificity the author rule wins by source order; to override a more-specific canonical rule a skill matches its specificity (or just adds new properties). The contract text (Phase 2) advertises this model so skills expect it. `@layer` is NOT adopted — it would only help a low-specificity rule wholesale-override a more-specific base rule, which is the opposite of "modify a bit"; it stays a noted fallback if specificity collisions ever prove painful.

Injected in `mode == "native"` only — never in `diff` or `synthesis-native` (R1, item 6). *Verify `@scope` support in the target browser during Phase 3; it is broadly supported in current Chromium/Safari/Firefox.*

### 4. Scope-and-keep is render-time, not a migration

The 7 opaque docs keep their stored `<style>` in the DB unchanged; on render we extract and scope it. A new pure function in `doc_render.py` returns both the cleaned inner body and the gathered author CSS:

```
def extract_safe_inner_with_css(html: str) -> tuple[Markup, str]:
    # Returns inner HTML of <main class="document">/<body> plus gathered author CSS.
    # <style> text is collected (not dropped); unscopable document-level at-rules
    # (@import, @charset, @namespace) are removed; <script>/<head> still
    # stripped. Best-effort — anything that cannot be scoped is dropped, not bled.
```

**Implementation note:** today's `_SafeInnerParser` *skips* the `<style>` subtree entirely (it sets a skip flag and discards the inner data). Gathering the text needs a NEW capture path for `<style>` content (collect `handle_data` while inside it), kept distinct from the `<script>`/`<head>` skip — so this is a new function/parser, not an edit to the shared `extract_safe_inner` (which stays for body-only callers). The render path uses the gathered CSS the same way as `extra_css`.

**Containment hardening:** before emitting any gathered or `extra_css` string into a `<style>` element, neutralise a literal `</style>` (and `<!--`) in it — otherwise author CSS could close the style element early and dump the remainder into the document as markup. Not a security concern (self-authored localhost) but a real containment correctness bug.

### 5. Usage logged as an `extra_css_used` event

Reuses the existing `events` stream. In `submit_document`, when a version is cut (the INSERT branch and the changed branch) **and** `content.extra_css` is non-empty, emit one `extra_css_used` event. A byte-identical no-op write cuts no version and fires no event. Absent/empty extra_css never fires.

### 6. Contract served at a stable URL; manifest points at it

`doc.css` is already served by the `/static` mount; the stable contract URL is `/static/doc.css` (no `?v=` cache-buster — skills fetch fresh each run). `get_manifest` gains a `presentation` block pointing at it and advertising the `extra_css` affordance.

## Data model

**No schema migration.** `extra_css` rides inside the existing `document_versions.content_json` blob via `serialise()` (Key decision 1), so it versions with content and needs no new column or table.

- `ParsedContent` gains `extra_css: str = ""`.
- `serialise()` includes the key only when non-empty → existing rows' serialised form is unchanged (no spurious version churn, byte-equality key stable).
- `current_content` / `content_at_or_before` decode with `data.get("extra_css", "")` — old rows (no key) yield `""`.
- `events`: a new `event_type` value `"extra_css_used"`, payload `{"type": doc_type, "feature": feature}` (matching the existing created/updated payload shape). No schema change — `event_type` is free text.

**Inbox/classification check:** confirm the new event type is ignored by `storage/inbox.py` reason-classification (it should fall through as a non-content event and not mis-flag the doc); add a guard/test if needed.

## Contract

**PUT `/api/documents/{project}/{feature}/{doc_type}/{instance}`** — accept an optional top-level `"extra_css": string` alongside `sections`/`body`:

```
{ "sections": { "...": "..." }, "extra_css": "@scope-able author css", "actor": "agent" }
```

- Validation: if present, must be a string and ≤ 1 MB (else 400 via `SubmitError`); empty/whitespace-only is accepted and treated as none.
- `?dry_run=true` validates extra_css too (no write).
- The GET document response should echo `extra_css` (so a re-PUT round-trips without dropping it).

**GET `/api/manifests/{doc_type}`** — add a presentation pointer (same for every doc_type; no DB needed):

```
{ "doc_type": "...", "shape": "...", "sections": [...], "repeated_prefixes": [...],
  "presentation": {
    "stylesheet_url": "/static/doc.css",
    "extra_css": "Optional top-level field on document writes. Scoped to the document
                  body and flagged for review. Base stylesheet rules still apply per-
                  property; extra_css layers on top — to adjust an existing rule, match
                  its specificity (or add new properties). Use only when the stylesheet
                  vocabulary doesn't cover what you need." } }
```

Note: `/static/doc.css` (bare) is the stable contract URL for skills; the rendered page separately loads `doc.css?v={mtime}` for browser cache-busting. Two consumers, one asset — don't remove the template's `?v=`.

**GET `/static/doc.css`** — the contract itself (stable URL, already mounted; no code change beyond keeping the path stable).

## File structure

**Phase 1**

- `web/static/doc.css` — widen + comment (modify)
- `web/static_assets_test.py` — new coverage-guard test (create; name to match existing `*_test.py` colocation convention)

**Phase 2**

- `web/submit.py` — `get_manifest` gains the `presentation` block (modify)
- `web/submit_test.py` (or the existing manifest test file) — assert the pointer (modify/create)

**Phase 3**

- `storage/doc_content.py` — `ParsedContent.extra_css`, `serialise()` (modify)
- `storage/versions.py` — decode `extra_css` in both readers (modify)
- `storage/documents.py` — `build_content` param + validation; `submit_document` event (modify)
- `storage/doc_render.py` — `extract_safe_inner_with_css` + at-rule filtering (modify)
- `web/submit.py` — `put_document` reads top-level `extra_css`; `get_document` echoes it (modify)
- `web/doc_view.py` — thread scoped CSS to the template in native mode (modify)
- `web/templates/doc.html` — render the scoped `<style>` in the native block (modify)
- Colocated `*_test.py` for each: doc_content, versions, documents, doc_render, submit, doc_view (modify/create)

## Verification

Run from the repo root. Primary gate is the full suite; the targeted commands fail loudly when a phase's feature is absent.

**Full QA gate (all phases):**

```
uv run ruff format --check .
uv run ruff check .
uv run ty check .
uv run pytest
```

**Phase 1** — coverage guard + spot-check the bug fix:

```
uv run pytest -k "css_coverage or static_assets"
grep -Eq 'th[ ,{]|\btd\b|table' feature_skills_webapp/web/static/doc.css && echo "table rules present"
```

**Phase 2** — contract pointer + stylesheet served (requires the service running on :8800, or run the equivalent Starlette TestClient test):

```
uv run pytest -k "manifest and presentation"
curl -fsS http://127.0.0.1:8800/api/manifests/requirements | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['presentation']['stylesheet_url']=='/static/doc.css'; print('ok')"
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8800/static/doc.css   # expect 200
```

**Phase 3** — behaviour suite + a live round-trip `(Note: the curl round-trip requires the service running on :8800; per CLAUDE.md the deployed service won't reflect edits until you reinstall+restart it first)`:

```
uv run pytest -k "extra_css or scope_and_keep or scoped"
# refresh the running service so the live checks exercise the new code:
uv tool install --editable . --reinstall && systemctl --user restart feature-skills-webapp
# live: PUT a throwaway doc with extra_css, confirm it round-trips and renders scoped
curl -fsS -X PUT "http://127.0.0.1:8800/api/documents/feature-skills-webapp/_vtest/requirements/1" \
  -H 'Content-Type: application/json' \
  -d '{"sections":{"problem":"<p>x</p>"},"extra_css":"table{border:1px solid red}","actor":"agent"}'
curl -fsS "http://127.0.0.1:8800/api/documents/feature-skills-webapp/_vtest/requirements/1" | grep -q extra_css && echo "extra_css round-trips"
```

## Qc

Follow whatever `CLAUDE.md` specifies at implementation time. As of now the gate is, from the repo root, all of:

```
uv run ruff format .      # or: uv run ruff format --check .  (CI)
uv run ruff check .
uv run ty check .
uv run pytest             # xdist + pytest-socket; per-worker DB
```

All must pass before each phase's MR. New tests must be able to fail for the right reason (per the project's testing rules): confirm each new test goes red without the phase's change.

## Checklist

### Phase 1: Widen & document doc.css

- Add commented table/th/td rules to doc.css (the reported bug).
- Add h4, blockquote, hr, dl/dt/dd rules, each commented with intended use.
- Add semantic-class rules: .stories/.actor/.want/.scenario, .alternatives/.alt-*, .vision-statement, .questions.
- Write the coverage-guard test asserting the curated selectors exist in doc.css; confirm it goes red without the rules.
- Run the full QA gate; open MR 1.

### Phase 2: Serve & advertise the contract

- Add the constant presentation block (stylesheet_url + extra_css affordance) to get_manifest.
- Confirm /static/doc.css resolves at the stable (no-query) URL.
- Test the manifest pointer and that /static/doc.css returns 200 CSS.
- Run the full QA gate; open MR 2.

### Phase 3: Scoped extra_css + scope-and-keep + logging

- Add extra_css to ParsedContent and serialise() (key only when non-empty); decode in both versions.py readers.
- Add extra_css param + validation to build_content; whitespace-only normalises to "".
- put_document reads top-level extra_css; get_document echoes it.
- Emit extra_css_used event in submit_document's INSERT and changed branches when extra_css is non-empty.
- Add extract_safe_inner_with_css to doc_render.py (new style-capture path; drop @import/@charset/@namespace; keep script/head stripping; neutralise literal in gathered/extra CSS).
- Thread scoped_css to the template in doc_view native mode only (not diff/synthesis).
- Render the @scope (#doc-main) style block in doc.html's native block, after the doc.css link.
- Confirm inbox.py does not mis-classify the new extra_css_used event; guard/test if needed.
- Write tests: no-chrome-bleed, scope-and-keep + @import drop, event exactness, render-mode boundary, round-trip; confirm each fails without the change.
- Manually confirm @scope renders correctly in the browser.
- Run the full QA gate; open MR 3.

## Phase 1

**Build.** Add commented rule blocks to `web/static/doc.css` for the vocabulary the corpus emits, matching the existing dark-theme variables (`--surface`, `--border`, `--text`, `--accent`, …):

- `table` / `thead` / `tbody` / `tr` / `th` / `td` — borders, padding, header weight/background (the reported bug).
- `h4` (h1–h3 already styled), `blockquote`, `hr`, `dl`/`dt`/`dd`.
- Semantic classes: `ol.stories` + `.actor`/`.want`/`.scenario` cards; `ol.alternatives` + `.alt-title`/`.alt-source`/`.alt-reason`; `.vision-statement`; `.questions`.

Each block carries a comment naming its intended use (the contract guide), e.g. `/* User-story cards: <ol class="stories">><li> with .actor/.want/.scenario */`.

**Test.** A coverage-guard test reads `doc.css` and asserts a curated list of selectors/classes is present (substring or simple regex per token), so a future deletion regresses loudly. Keep the list to what this phase adds; broad emitted-vocabulary scanning is deliberately NOT automated (left to the retro ratchet).

**MR 1** — CSS + one test. No schema, no API, no Python behaviour change.

## Phase 2

**Build.** In `web/submit.py`, extend `get_manifest`'s JSON with a constant `presentation` block (see HTTP contract) — a `stylesheet_url` of `/static/doc.css` and an `extra_css` affordance string. The stylesheet is already served by the `/static` mount; confirm `/static/doc.css` (no query string) resolves, and treat that path as the stable contract URL.

**Test.** Assert `GET /api/manifests/requirements` (and one other type) returns the `presentation.stylesheet_url` and the affordance text; assert `GET /static/doc.css` returns 200 with CSS content.

**MR 2** — read-only; no stored state. Skill-side consumption is the separate `feature-skills` repo (out of scope; see context `feature-skills/doc-presentation-contract-skills/context/1`).

## Phase 3

**Storage.** Add `extra_css: str = ""` to `ParsedContent`; update `serialise()` to include it only when non-empty (Key decision 1); decode it in `current_content` and `content_at_or_before`.

**Write path.** `build_content(doc_type, sections, body, extra_css)` — normalise whitespace-only to `""`, enforce `MAX_BODY_BYTES`, attach to the returned `ParsedContent`. `put_document` reads `body.get("extra_css")` and passes it through; `get_document` echoes `extra_css`. In `submit_document`, in the INSERT and changed branches, emit one `extra_css_used` event when `content.extra_css` is non-empty.

**Render path.** Add `extract_safe_inner_with_css(html) -> (Markup, str)` to `doc_render.py` (new capture path for `<style>` text — see Key decision 4; drop unscopable document-level at-rules; still strip `<script>`/`<head>`; neutralise a literal `</style>` in the gathered CSS). In `doc_view.doc_shell`, for `mode == "native"` only, compute `scoped_css` = the doc's `content.extra_css` (section docs) or the gathered CSS (opaque docs), and pass it to the template. Diff and synthesis-native paths pass nothing.

Note opaque docs do NOT carry an `extra_css` field (their write contract uses `body`; `build_content` forbids it), so their only scoped CSS is the gathered `<style>` and scope-and-keep recovery fires **no** `extra_css_used` event — the event keys off `content.extra_css` only.

**Template.** In the `native` block of `doc.html`, when `scoped_css` is set, emit `<style>@scope (#doc-main) { {{ scoped_css|safe }} }</style>` after the `doc.css` link. (The `|safe` is why the `</style>` neutralisation in the render path is load-bearing.)

**Tests.**

- **No chrome bleed (assert behaviour, not construction):** extra_css containing a stray `}` followed by a chrome selector (e.g. `} .crumbs { display:none }`) must NOT escape the scope — assert the rendered output cannot produce an out-of-`#doc-main` rule (e.g. the chrome selector stays inside the `@scope` block / is neutralised), and that a literal `</style>` in the CSS does not terminate the style element. Per TESTING.md rule 3, assert the observable containment, not merely that a wrapper is present.
- **Scope-and-keep:** an opaque body with a `<style>` block and an `@import` renders with the style content scoped and the `@import` dropped; `<script>` still stripped.
- **Event exactness:** a write with non-empty extra_css emits exactly one `extra_css_used` event; absent/empty/whitespace-only emits none; a byte-identical re-PUT emits none. Confirm scope-and-keep recovery of an opaque doc emits none.
- **Render-mode boundary:** a **genuine** diff render (two versions with a real textual change, so `mode` resolves to `diff` — NOT merely the `?view=diff` param, which can fall back to `native`) and a synthesis-native render do NOT contain the scoped style block.
- **Round-trip:** PUT with extra_css → GET echoes it → re-PUT identical → `changed=False`, no new version, no event.

**MR 3** — touches write + render paths; ships last. Manually confirm `@scope` renders correctly in the browser before merge.
