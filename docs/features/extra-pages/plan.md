# extra-pages

## Overview

Four inbox/navigation increments, each a standalone MR, all read-model and presentation over the existing schema — no migration. Phase 1 gives inbox doc-type badges distinct colours. Phase 2 adds a "Mark all read" control that clears exactly the "New since last visit" set for the active filter. Phases 3 and 4 add readable per-feature and per-project pages, wired into the inbox cards, the doc-view breadcrumbs, and the existing SSE channel, completing the inbox → project → feature → doc drill-down.

## Key technical decisions

1. **Badge class = normalised doc-type slug, carried on the card**
  `InboxCard` gains a `badge: str` field set at construction. For document cards it's the doc type normalised so all feedback variants collapse to one bucket; for the synthetic categories it's a fixed token. The template emits `badge-{{ card.badge }}` on the existing `.card-label` span; unknown values match no rule and fall back to the base amber styling. Colour *and* the existing text label both differentiate — never colour alone.
  ```python
  def badge_kind(doc_type: str | None) -> str:
      # None never reached for doc cards; defensive default.
      if doc_type is None:
          return "context"
      if doc_type.endswith(FEEDBACK_SUFFIX):  # e.g. "requirements-feedback"
          return "feedback"
      return doc_type  # context | requirements | plan | review | (unknown)

  # _doc_card:     badge=badge_kind(r["doc_type"])
  # in_progress:   badge="in-progress"
  # _shipped_card: badge="shipped"
  ```
  Committed colour map (CSS in `index.html`): context amber (base), requirements blue, plan violet, review teal, feedback magenta, shipped green (the existing done-accent `#4ade80`), in-progress neutral grey.
2. **Mark-read stamps exactly the "New since last visit" set**
  Reuse the inbox's own unread query rather than `mark_all_read` (which over-stamps every active doc). `inbox.mark_new_since_read` takes the cards `new_since_last_visit` already produces (which excludes "Awaiting your input") and delegates the write to a new `read_state.mark_documents_read`. inbox→read_state is a one-way import (read_state imports only `db`), so no cycle.
  ```python
  # storage/read_state.py
  def mark_documents_read(conn: sqlite3.Connection, document_ids: list[int]) -> int:
      now = now_iso()
      with transaction(conn):
          for doc_id in document_ids:
              conn.execute(
                  "INSERT INTO read_state (document_id, last_read_at) VALUES (?, ?) "
                  "ON CONFLICT(document_id) DO UPDATE SET last_read_at = excluded.last_read_at",
                  (doc_id, now),
              )
      return len(document_ids)

  # storage/inbox.py
  def mark_new_since_read(conn: sqlite3.Connection, project_id: int | None = None) -> int:
      ids = [c.document_id for c in new_since_last_visit(conn, project_id) if c.document_id]
      return mark_documents_read(conn, ids)
  ```
  New endpoint `POST /admin/mark-read` with optional `?project=<name>` (omitted = all projects); returns `{"stamped": N}` for the confirmation. Unknown project → 404. The existing per-project route is left untouched.
3. **Inbox cards: individual links, not one wrapping anchor**
  Today a new-since (or awaiting-input) card is a single `<a href="/doc/{id}">` wrapping the whole row. To give the project name, feature name and doc their own destinations without nesting anchors, the card becomes a non-anchor `.card` whose children are individual links: project name → project page, feature name → feature page, and the doc-type badge → `/doc/{id}` (only when the card has a `document_id` — so both the new-since and awaiting-input cards are in scope). Synthetic cards (in-progress, shipped) have no badge link but still linkify their names. Only opening the doc stamps read; the name links do not.
  **Accessibility:** the accessible name currently lives on the wrapping `<a>` (its `aria-label`); the restructure drops it, so each link needs its own — e.g. the badge link `aria-label="Open {feature} {label}"`, the name links labelled by their text. The current `a.card-link:focus-visible` rule must migrate to the new per-link anchors so keyboard focus stays visible, and the existing `aria-label=` assertion test must be updated rather than passing incidentally on a different element.
  **Phasing:** the link wiring is split — Phase 3 adds the feature-name link (feature page exists then) and leaves project names as plain text; Phase 4 adds the project-name link. So no project-name link 404s before the project page ships. The card may restructure into individual elements in Phase 3 with the project name rendered as a non-link span until Phase 4 upgrades it.
4. **Readable routing, 404 on unknown**
  Pages are keyed by readable identifiers: `/project/{project}` and `/project/{project}/feature/{slug}`. Starlette decodes path params on the way in; links build them with the project name + slug (slugs are unique only within a project) and **must** run both segments through Jinja's `| urlencode` filter — the existing chips emit unencoded `href`s, so there's no pattern to copy. Unknown project/feature → **404** (an addressed landmark that should exist), unlike the inbox filter's degrade-to-empty.
5. **Breadcrumb hrefs populated incrementally**
  `doc_view.breadcrumbs` already returns `(label, href|None)` tuples, and `doc.html` *already* renders a crumb as a link when its href is present (the `{% if href %}<a>…{% else %}<span>` branch is in the template today). So the **only** change is populating the href slot in `breadcrumbs()` — no `doc.html` edit is needed (just new href-coverage tests). The feature crumb gains its href in Phase 3 (feature page exists then); the project crumb — including the tracker-doc path's leading project crumb — gains its href in Phase 4. This keeps every phase's links valid on landing.
6. **SSE on pages via debounced reload, reusing the inbox pattern**
  Feature and project pages subscribe to `/events` and reload themselves (250 ms debounce, plus a `visibilitychange` recheck), mirroring the inbox's `EventSource` client. A full `location.reload()` is sufficient — these pages are small and the broadcast is contentless; no fragment endpoint needed.
  **Must avoid a reload loop:** `/events` yields an initial `changed` message on every connect (`events.py` emits it before registering). The inbox is immune because it only refetches a fragment, but a page that `location.reload()`s on that first message would reconnect, receive it again, and loop. So the page must **ignore the first message after (re)connect** — gate the reload behind a "have I seen a message since this page loaded" flag (skip the first `onmessage`), reloading only on genuine subsequent changes.
  The mark-read button (Phase 2) is a separate path: read-state changes don't broadcast, so it refetches the inbox itself.

## File structure

### New files

- `feature_skills_webapp/web/feature_page.py` — feature-page handler (Phase 3).
- `feature_skills_webapp/web/feature_page_test.py` — its tests.
- `feature_skills_webapp/web/templates/feature.html` — feature-page template.
- `feature_skills_webapp/web/project_page.py` — project-page handler (Phase 4).
- `feature_skills_webapp/web/project_page_test.py` — its tests.
- `feature_skills_webapp/web/templates/project.html` — project-page template.

### Modified files

- `storage/inbox.py` — `badge` field + `badge_kind` (P1); `mark_new_since_read` (P2).
- `storage/inbox_test.py` — badge + mark-new-since coverage.
- `storage/read_state.py` — `mark_documents_read` (P2).
- `storage/read_state_test.py` — its coverage.
- `web/routes.py` — `admin_mark_new_since_read` (P2).
- `web/routes_test.py` — endpoint coverage.
- `web/app.py` — register the mark-read, feature-page, project-page routes.
- `web/doc_view.py` — breadcrumb hrefs (P3 feature, P4 project).
- `web/doc_view_test.py` — crumb-href coverage.
- `web/templates/_inbox_body.html` — badge class + card restructure + name links + mark-read button.
- `web/templates/index.html` — badge CSS + mark-read JS.
- `web/templates/doc.html` — render crumb hrefs as links.

## Phase 1 — Differentiated doc-type badges

### What's built

Each inbox card carries a normalised `badge` token; the template keys a per-type CSS class off it; `index.html` gains the colour rules. Unknown types fall back to amber. No new routes.

### Key code

```python
@dataclass(frozen=True)
class InboxCard:
    project: str
    feature: str | None
    label: str
    last_activity: str | None
    document_id: int | None = None
    badge: str = "context"   # normalised slug for the per-type CSS class
```

`_doc_card` sets `badge=badge_kind(r["doc_type"])`; `_feature_card` for in-progress sets `badge="in-progress"`; `_shipped_card` sets `badge="shipped"`. Template span becomes `<span class="card-label badge-{{ card.badge }}">` in all four categories.

### Tests

- `inbox_test.py`: a requirements doc card has `badge == "requirements"`; a `*-feedback` doc normalises to `"feedback"`; in-progress and shipped cards get their fixed tokens.
- `routes_test.py` (or an index render test): the rendered inbox HTML contains `badge-requirements` / `badge-plan` for the seeded docs.

### MR chain

One MR titled `feat(extra-pages): phase 1 — doc-type badges`.

## Phase 2 — "Mark all read" on New since last visit

### What's built

`mark_documents_read` (read_state) + `mark_new_since_read` (inbox); a `POST /admin/mark-read` endpoint scoped by optional `?project=`; a button in the "New since last visit" section header; a delegated click handler that POSTs, shows "Marked N as read", and refetches the inbox.

### Key code

```python
# web/routes.py
async def admin_mark_new_since_read(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.query_params.get("project")
    with request_conn(request.app) as conn:
        project_id = None
        if project is not None:
            row = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()
            if row is None:
                return JSONResponse({"error": "unknown project"}, status_code=404)
            project_id = row["id"]
        stamped = mark_new_since_read(conn, project_id)
    return JSONResponse({"stamped": stamped})
```

Route: `Route("/admin/mark-read", admin_mark_new_since_read, methods=["POST"])`. Button lives in the refetched fragment, so the handler is **delegated** from the stable `#inbox-body` in `index.html` (a directly-bound listener would be lost on refetch). It reads the active `project` from `window.location.search` and calls the existing `refetchInbox()` on success.

### Tests

- `read_state_test.py`: `mark_documents_read` upserts each id and returns the count; empty list is a no-op.
- `inbox_test.py`: `mark_new_since_read` stamps the new-since docs but leaves an "Awaiting your input" feedback doc unread; project filter scopes it.
- `routes_test.py`: POST with no project stamps all; with `?project=` scopes; unknown project → 404; 503 when db unconfigured; response carries `stamped`.

### MR chain

One MR titled `feat(extra-pages): phase 2 — mark all read`.

## Phase 3 — Feature page

### What's built

A `GET /project/{project}/feature/{slug}` page: feature status/owner/notes header, primary docs in `DOC_TYPE_ORDER` (each badge linking to `/doc/{id}`), a grouped feedback subsection (unanswered ones badged "Awaiting your input"), and a de-emphasised archived subsection. Inbox card feature-names link here; the doc-view feature breadcrumb gains its href; the page live-refreshes on SSE.

### Key code

```python
# web/feature_page.py — sketch
async def feature_page(request: Request) -> Response:
    app = request.app
    if app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    project = request.path_params["project"]
    slug = request.path_params["slug"]
    with request_conn(app) as conn:
        feat = conn.execute(
            "SELECT f.id, f.slug, f.status, f.owner, f.notes, p.name AS project "
            "FROM features f JOIN projects p ON f.project_id = p.id "
            "WHERE p.name = ? AND f.slug = ?",
            (project, slug),
        ).fetchone()
        if feat is None:
            return PlainTextResponse("Not found", status_code=404)
        docs = conn.execute(
            "SELECT id, type, status FROM documents WHERE feature_id = ?",
            (feat["id"],),
        ).fetchall()
        # partition: primary active (type in DOC_TYPE_ORDER) sorted by _rank,
        #            active feedback (type LIKE %FEEDBACK_SUFFIX) — awaiting flag
        #            via NOT EXISTS synthesis_responses, archived (status='archived')
        #            sorted by (_rank(type), id) — id is monotonic, no timestamp
        #            column is selected so use it as the recency tiebreak.
    return app.state.templates.TemplateResponse(request, "feature.html", {...})
```

Reuse `_rank` / `DOC_TYPE_ORDER` from the existing modules. "Awaiting your input" per feedback doc = no row in `synthesis_responses` for that `document_id` (the same predicate the inbox uses). Breadcrumb on the page: `Project ▸ Feature` (project crumb is plain text until Phase 4 adds the project page). In `doc_view.breadcrumbs`, set the feature crumb href to `/project/{project}/feature/{slug}` and update `doc.html` to render crumbs with an href as links. Inbox: in `_inbox_body.html`, restructure cards per decision 3 and link the feature name to the feature page across all categories.

### Tests

- `feature_page_test.py`: primary docs render in type order with links; feedback grouped, unanswered one badged awaiting; archived in its own subsection; a feature with no docs renders "no docs yet"; unknown project/slug → 404; 503 when unconfigured.
- `doc_view_test.py`: a doc's feature breadcrumb now carries the feature-page href.
- `routes_test.py`/inbox render: a card's feature name links to `/project/.../feature/...`; opening the doc still stamps read but the name links don't.

### MR chain

One MR titled `feat(extra-pages): phase 3 — feature page`.

## Phase 4 — Project page

### What's built

A `GET /project/{project}` page listing features grouped by status (In progress / Available / Done), each linking to its feature page, plus the project's tracker doc (the `feature_id = NULL` doc) if present. Inbox card project-names link here; the doc-view project breadcrumb gains its href; the page live-refreshes on SSE. Completes the drill-down.

### Key code

```python
# web/project_page.py — sketch
async def project_page(request: Request) -> Response:
    app = request.app
    if app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)
    name = request.path_params["project"]
    with request_conn(app) as conn:
        proj = conn.execute("SELECT id, name FROM projects WHERE name = ?", (name,)).fetchone()
        if proj is None:
            return PlainTextResponse("Not found", status_code=404)
        feats = conn.execute(
            "SELECT f.slug, f.status, f.owner, f.notes, "
            "  (SELECT MAX(e.created_at) FROM events e "
            "   JOIN documents d ON e.document_id = d.id "
            "   WHERE d.feature_id = f.id AND d.status='active') AS last_activity "
            "FROM features f WHERE f.project_id = ? ORDER BY f.status, f.slug",
            (proj["id"],),
        ).fetchall()
        tracker = conn.execute(
            "SELECT id FROM documents WHERE project_id = ? AND feature_id IS NULL "
            "AND status = 'active'",
            (proj["id"],),
        ).fetchone()
    # group feats into in_progress / available / done for the template
    return app.state.templates.TemplateResponse(request, "project.html", {...})
```

Statuses are `in_progress | available | done` (walker.py). A feature with no activity still lists (last_activity may be null). In `doc_view.breadcrumbs`, set the project crumb href to `/project/{project}`. Inbox: link the card project-name to the project page across all categories; the null-feature tracker card links only its project name (no feature link).

### Tests

- `project_page_test.py`: features grouped by status with feature-page links; tracker doc linked when present; an available-only project renders the list (not an empty state); unknown project → 404; 503 when unconfigured.
- `doc_view_test.py`: a doc's project breadcrumb now carries the project-page href.
- inbox render: a card's project name links to `/project/...`.

### MR chain

One MR titled `feat(extra-pages): phase 4 — project page`.

## QC

There's no `CLAUDE.md` in this repo; follow the established project conventions the shipped features used: `uv run pytest` (xdist + pytest-socket, per-worker DB), `uv run ruff check` + `uv run ruff format`, and `uv run ty check` — all clean before each commit. Tests use `starlette.testclient.TestClient` over `create_app(db_path, docs_root)` with seeded HTML docs and a forced walk, per the existing `*_test.py` patterns. If a runtime dependency is added, reinstall the tool per the README note.

## Checklist

### Phase 1: Doc-type badges

- Add `badge: str = "context"` to `InboxCard` and a `badge_kind(doc_type)` helper in `storage/inbox.py`.
- Set `badge` in `_doc_card` (via `badge_kind`), in-progress (`"in-progress"`), and `_shipped_card` (`"shipped"`).
- Emit `badge-{{ card.badge }}` on the `.card-label` span in all four categories of `_inbox_body.html`.
- Add the per-type colour CSS (context/requirements/plan/review/feedback/shipped/in-progress) to `index.html`, base amber as the fallback.
- Tests: badge tokens in `inbox_test.py`; rendered badge classes in the inbox HTML.
- QC, then open MR `feat(extra-pages): phase 1 — doc-type badges`.

### Phase 2: Mark all read

- Add `mark_documents_read(conn, document_ids)` to `storage/read_state.py`.
- Add `mark_new_since_read(conn, project_id=None)` to `storage/inbox.py`, reusing `new_since_last_visit`.
- Add `admin_mark_new_since_read` to `web/routes.py` and register `POST /admin/mark-read` in `web/app.py`.
- Add the "Mark all read" button to the New-since section header in `_inbox_body.html`.
- Add a delegated click handler in `index.html` (bound to `#inbox-body`) that POSTs with the active project, shows "Marked N as read", and calls `refetchInbox()`.
- Tests: `read_state_test.py` (mark_documents_read), `inbox_test.py` (new-since only, awaiting-input untouched, project scope), `routes_test.py` (all/scoped/404/503/count).
- QC, then open MR `feat(extra-pages): phase 2 — mark all read`.

### Phase 3: Feature page

- Create `web/feature_page.py` with the `GET /project/{project}/feature/{slug}` handler (partition primary/feedback/archived; awaiting-input flag; 404/503).
- Create `web/templates/feature.html` (status/owner/notes header, doc subsections with badge→doc links, `Project ▸ Feature` breadcrumb, SSE reload script that *suppresses the first message after connect* to avoid a reload loop).
- Register the route in `web/app.py`.
- Populate the feature-crumb href in `doc_view.breadcrumbs` — `doc.html` already renders crumb hrefs as links, so no template edit (add href-coverage tests only).
- Restructure inbox cards (decision 3) for the new-since + awaiting-input categories, link the feature name to the feature page (project name stays plain text until Phase 4), add per-link aria-labels, and migrate `:focus-visible` off `a.card-link` in `index.html`.
- Tests: `feature_page_test.py` (ordering, feedback grouping, awaiting badge, archived, no-docs, 404/503); `doc_view_test.py` (feature crumb href); inbox render (feature-name link, read-state untouched by name links).
- QC, then open MR `feat(extra-pages): phase 3 — feature page`.

### Phase 4: Project page

- Create `web/project_page.py` with the `GET /project/{project}` handler (group by status; tracker doc lookup; 404/503).
- Create `web/templates/project.html` (In progress / Available / Done groups with feature-page links, tracker link, breadcrumb, SSE reload script with the same first-message suppression as the feature page).
- Register the route in `web/app.py`.
- Set the project-crumb href in `doc_view.breadcrumbs`.
- Link the inbox card project-name to the project page across all categories; null-feature tracker card links project name only.
- Tests: `project_page_test.py` (status grouping, feature links, tracker link, available-only, 404/503); `doc_view_test.py` (project crumb href); inbox render (project-name link).
- QC, then open MR `feat(extra-pages): phase 4 — project page`.
