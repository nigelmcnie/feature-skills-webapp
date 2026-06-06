# doc-view

## Overview

Add a `web/doc_view.py` module with two routes: a shell at `/doc/{id}` that wraps a doc in webapp chrome (breadcrumbs + view-source link, plus sibling nav in phase 2) and stamps `read_state.last_read_at` on render, and a content endpoint at `/doc/{id}/raw` that streams the indexed file's bytes into an iframe so the doc's own CSS and JS (TOC, highlighting, click-to-comment) run untouched. Wire the inbox cards in `index.html` to link to their doc. Phase 1 delivers the full headline loop (click a card → read in-tab → it leaves "New since last visit"); phase 2 adds prev/next navigation across a feature's docs. No schema change, no migration.

## Key technical decisions

1. **Two routes, addressed by integer document id**
  The shell and the content endpoint are both keyed by the `documents.id` primary key — already carried on each `InboxCard`. Starlette's `:int` converter gives a free 404 on non-numeric ids and avoids collision with the literal routes. Register in `web/app.py` alongside the existing routes:
  ```python
  Route("/doc/{document_id:int}", doc_shell),
  Route("/doc/{document_id:int}/raw", doc_raw),
  ```
2. **One lookup join for identity; stamp read-state after the read**
  The shell handler resolves the doc and its breadcrumb identity in one query (LEFT JOIN `features` because the tracker doc has no feature), then calls `mark_read`. `mark_read` opens its own `transaction()`, so it is called *after* the read query completes — never nested inside another transaction.
  ```python
  ROW_SQL = (
      "SELECT d.id, d.type, d.status, d.source_path, d.content_html, "
      "  p.name AS project, f.slug AS feature "
      "FROM documents d "
      "JOIN projects p ON d.project_id = p.id "
      "LEFT JOIN features f ON d.feature_id = f.id "
      "WHERE d.id = ?"
  )

  async def doc_shell(request: Request) -> Response:
      app = request.app
      if app.state.db_path is None:
          return JSONResponse({"error": "db not configured"}, status_code=503)
      doc_id = request.path_params["document_id"]
      with request_conn(app) as conn:
          row = conn.execute(ROW_SQL, (doc_id,)).fetchone()
          if row is None:
              return PlainTextResponse("Not found", status_code=404)
          crumbs = breadcrumbs(row)
          available = row["status"] in ("active", "archived")
          mark_read(conn, doc_id)  # own transaction; after the read
      return app.state.templates.TemplateResponse(
          request, "doc.html",
          {"doc_id": doc_id, "crumbs": crumbs, "available": available,
           "raw_url": f"/doc/{doc_id}/raw", "siblings": None},  # siblings filled in phase 2
      )
  ```
3. **Content endpoint serves indexed docs only; prefers content_html (Stage 2 seam)**
  The raw endpoint looks the doc up by id and reads its recorded `source_path` — never a caller path, so there's no traversal surface. It prefers `content_html` if ever populated (the Stage-2 forward seam), otherwise reads the file. Archived docs are served as well as active ones; unreadable / missing files and unknown ids return 404.
  ```python
  async def doc_raw(request: Request) -> Response:
      app = request.app
      if app.state.db_path is None:
          return PlainTextResponse("Not found", status_code=404)
      doc_id = request.path_params["document_id"]
      with request_conn(app) as conn:
          row = conn.execute(
              "SELECT status, source_path, content_html FROM documents WHERE id = ?",
              (doc_id,),
          ).fetchone()
      if row is None or row["status"] not in ("active", "archived"):
          return PlainTextResponse("Not found", status_code=404)
      if row["content_html"]:
          return HTMLResponse(row["content_html"])
      if not row["source_path"]:
          return PlainTextResponse("Not found", status_code=404)
      try:
          html = Path(row["source_path"]).read_text(encoding="utf-8", errors="replace")
      except OSError:
          return PlainTextResponse("Not found", status_code=404)
      # comment-capture seam: a later feature injects the real document_id into the
      # served HTML here (replacing its path-string docId) so the widget can POST
      # comments. Distinct from the content_html (Stage-2) seam above.
      return HTMLResponse(html)
  ```
4. **iframe fills the tab, scrolls internally, no sandbox**
  The shell template is a thin top bar over an iframe whose `src` is the raw URL. The iframe fills the viewport below the bar (`height: calc(100vh - barheight)`) and scrolls internally — the accepted Stage-1 model (round 1). It is served *without* a `sandbox` attribute: the content is our own loopback-served HTML and needs to load CDN highlight.js and run the click-to-comment script, both of which a restrictive sandbox would block.
  There's no base template in the repo (`index.html` is a standalone document), so `doc.html` is a complete HTML document with its own `<style>`. The bar is a fixed height; the iframe fills the rest via `calc(100vh - var(--bar-h))`. The view-source link shows only when the doc is available — in the unavailable state the raw endpoint 404s, so the link would be dead. Phase 1 renders *no* sibling markup; that's added in phase 2.
  ```html
  <!DOCTYPE html>
  <html lang="en"><head>
  <meta charset="UTF-8"><meta name="color-scheme" content="dark">
  <title>{{ crumbs[-1][0] }} — feature-skills-webapp</title>
  <style>
    :root { --bar-h: 48px; --bg:#1c1917; --surface:#292524; --border:#44403c;
            --text:#fafaf9; --text-faint:#a8a29e; --accent:#f59e0b; }
    * { box-sizing: border-box; }
    html, body { margin:0; padding:0; height:100%; background:var(--bg); color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
    .doc-bar { height:var(--bar-h); display:flex; align-items:center;
      justify-content:space-between; gap:16px; padding:0 16px;
      border-bottom:1px solid var(--border); background:var(--surface); }
    .crumbs { display:flex; align-items:center; gap:8px; font-size:0.9rem; min-width:0; }
    .crumbs .sep { color:var(--text-faint); }
    .crumbs a { color:var(--accent); text-decoration:none; }
    .view-source { color:var(--text-faint); font-size:0.85rem; text-decoration:none; white-space:nowrap; }
    .doc-frame { width:100%; height:calc(100vh - var(--bar-h)); border:0; display:block; }
    .unavailable { padding:48px; text-align:center; color:var(--text-faint); }
  </style></head><body>
  <header class="doc-bar">
    <nav class="crumbs">
      {% for label, href in crumbs %}
        {% if href %}<a href="{{ href }}">{{ label }}</a>{% else %}<span>{{ label }}</span>{% endif %}
        {% if not loop.last %}<span class="sep">/</span>{% endif %}
      {% endfor %}
    </nav>
    {% if available %}<a class="view-source" href="{{ raw_url }}" target="_blank">View source file</a>{% endif %}
  </header>
  {% if available %}
    <iframe class="doc-frame" src="{{ raw_url }}" title="document"></iframe>
  {% else %}
    <div class="unavailable">This document is no longer available.</div>
  {% endif %}
  </body></html>
  ```
5. **Breadcrumb helper, reusing humanise_type**
  A small pure function turns a joined row into an ordered list of `(label, href)` crumbs. Feature docs → `project / feature / Type`; the tracker (no feature, type `features`) → `project / Tracker`; archived docs get an "(archived)" suffix on the type label. It reuses `inbox.humanise_type` rather than re-deriving labels. Crumb hrefs are left as `None` in Stage 1 (display-only); they become links when project/feature views exist.
  ```python
  def breadcrumbs(row: sqlite3.Row) -> list[tuple[str, str | None]]:
      crumbs: list[tuple[str, str | None]] = [(row["project"], None)]
      if row["feature"] is None:        # project-level tracker doc
          crumbs.append(("Tracker", None))
          return crumbs
      crumbs.append((row["feature"], None))
      label = humanise_type(row["type"])
      if row["status"] == "archived":
          label += " (archived)"
      crumbs.append((label, None))
      return crumbs
  ```
6. **Inbox cards become accessible links**
  In `index.html`, a card that carries a `document_id` is wrapped in an anchor to `/doc/{id}` with an `aria-label` naming the doc; "In progress" and "Recently shipped" cards (no `document_id`) stay plain. A `:focus-visible` rule on the card link gives keyboard users a focus ring.
  ```html
  {% if card.document_id %}
  <li class="card">
    <a class="card-link" href="/doc/{{ card.document_id }}"
       aria-label="{{ card.project }} {{ card.feature }} {{ card.label }}">
      ... existing spans ...
    </a>
  </li>
  {% else %}
  <li class="card"> ... existing spans ... </li>
  {% endif %}
  ```
  The `new_since` category is the only one with `document_id` set today, so in practice only those cards link — exactly the docs a user wants to open.

## File structure

### New files

- `feature_skills_webapp/web/doc_view.py` — `doc_shell` and `doc_raw` handlers, the `ROW_SQL` lookup, and the `breadcrumbs` helper.
- `feature_skills_webapp/web/templates/doc.html` — the shell: top bar (breadcrumbs + view-source, sibling nav in phase 2) over the doc iframe; unavailable variant.
- `feature_skills_webapp/web/doc_view_test.py` — tests for both routes (alongside the module, matching the repo's `*_test.py` convention).

### Modified files

- `feature_skills_webapp/web/app.py` — register the two `/doc/...` routes; import the handlers.
- `feature_skills_webapp/web/templates/index.html` — wrap document-bearing cards in anchors; add `.card-link` + `:focus-visible` CSS.
- `feature_skills_webapp/storage/inbox.py` — *(phase 2)* add a canonical `DOC_TYPE_ORDER` next to `_TYPE_LABELS` as the single source of doc-type ordering.

`doc_view.py` opens with `from __future__ import annotations` (matching the other `storage/` and `web/` modules) and imports `sqlite3`, `pathlib.Path`, `starlette.requests.Request`, the needed `starlette.responses` (`HTMLResponse`, `JSONResponse`, `PlainTextResponse`, `Response`), `request_conn`, `mark_read`, and `inbox.humanise_type` (plus `DOC_TYPE_ORDER` in phase 2).

## Phase 1 — Open and read a doc, marked read

### What's built

The `/doc/{id}` shell and `/doc/{id}/raw` content endpoint, the `doc.html` shell template (breadcrumbs + view-source link + full-height internal-scroll iframe + unavailable variant), read-state stamping on shell render, and the graceful states (unknown id → 404, status-`missing` → "no longer available", db not configured → 503). The inbox cards in `index.html` become accessible links to their docs. End to end: click a card, read it in-tab, it leaves "New since last visit".

### Files touched

New: `web/doc_view.py`, `web/templates/doc.html`, `web/doc_view_test.py`. Modified: `web/app.py`, `web/templates/index.html`. See decisions 1–6 for the snippets.

### Tests

In `web/doc_view_test.py`, following the `routes_test.py` pattern (`TestClient(create_app(...))` with a `tmp_path` docs root, `POST /admin/discover` to index, then assert):

- Shell 200 for an indexed doc; response carries the breadcrumb parts (project, feature, humanised type) and an `<iframe>` whose `src` is `/doc/{id}/raw`.
- Viewing clears unread: a doc is in `unread_document_ids` before `GET /doc/{id}` and absent after — the headline-loop assertion.
- Unknown id → 404; non-numeric path (e.g. `/doc/abc`) → 404 via the `:int` converter.
- Raw endpoint returns the source file's HTML (assert a marker from the file body); unknown id and a doc with no `source_path` → 404.
- Viewing the raw endpoint does *not* stamp read-state: a doc stays unread after `GET /doc/{id}/raw` (only the shell stamps — guards the once-per-view invariant).
- Missing-file path: index a doc, delete the file, re-walk via a second `POST /admin/discover` (which walks with `reconcile=True`) so status flips to `missing` → shell renders "no longer available" (no iframe), raw → 404.
- Tracker doc (a `features.html`): shell renders with a `Tracker` crumb and no feature crumb; raw serves it.
- Archived doc (a file under `.feedback-archive/`): shell renders with the "(archived)" type label; raw serves it.
- db not configured → shell 503.
- Index template: `GET /` with an unread doc contains `href="/doc/{id}"` wrapping the card; an in-progress/shipped card (no document_id) is not wrapped in an anchor; the linked card has an `aria-label`.

### MR chain

One MR titled `feat(doc-view): phase 1 — render a doc and mark it read`.

## Phase 2 — Sibling-doc navigation

### What's built

Prev/next links across a feature's active docs in canonical lifecycle order, rendered in the shell's top bar. A single `DOC_TYPE_ORDER` constant in `inbox.py` (beside `_TYPE_LABELS`) is the one source of ordering. The shell computes the current doc's siblings and its neighbours; tracker docs (no feature) get no nav.

### Files touched

Modified: `storage/inbox.py` (add `DOC_TYPE_ORDER`), `web/doc_view.py` (compute siblings), `web/templates/doc.html` (render prev/next), `web/doc_view_test.py` (extend).

### Key logic

```python
# inbox.py — single source of doc-type ordering
DOC_TYPE_ORDER = ["context", "requirements", "plan", "review"]

# doc_view.py — neighbours of the current doc within its feature
def _rank(t: str) -> int:
    return DOC_TYPE_ORDER.index(t) if t in DOC_TYPE_ORDER else len(DOC_TYPE_ORDER)

def siblings(
    conn: sqlite3.Connection, feature_id: int, current_id: int
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    rows = conn.execute(
        "SELECT id, type FROM documents WHERE feature_id = ? AND status = 'active'",
        (feature_id,),
    ).fetchall()
    ordered = sorted(rows, key=lambda r: (_rank(r["type"]), r["id"]))  # id tie-break
    ids = [r["id"] for r in ordered]
    if current_id not in ids:        # archived / non-active current doc → no nav
        return None, None
    i = ids.index(current_id)
    prev = ordered[i - 1] if i > 0 else None
    nxt = ordered[i + 1] if i < len(ids) - 1 else None
    return prev, nxt
```

In `doc_shell`, call `siblings` only when `row["feature"]` is not None and the current doc is active; pass prev/next (id + humanised type) to the template and render them as `/doc/{id}` links, omitting whichever is absent. `siblings()` also returns `(None, None)` defensively if the current id isn't in the active set, so an archived feature doc renders without nav rather than crashing.

### Tests

- A feature with context + requirements + plan: the requirements shell links prev → context and next → plan (by `/doc/{id}`).
- First doc in order has no prev link; last has no next link.
- Ordering is independent of discovery/insertion order.
- A tracker doc renders with no sibling nav.
- An archived feature doc renders without sibling nav and without crashing (the `current_id not in ids` guard).

### MR chain

One MR titled `feat(doc-view): phase 2 — sibling-doc navigation`.

## QC

There is no `CLAUDE.md` in this repo; follow the README's **Development** section before each commit. Run, from the repo root, and ensure all pass clean:

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run ty check .
```

New code uses `from __future__ import annotations` and type hints consistent with the existing `web/` modules. SQL goes through the per-request `request_conn(app)` helper; writes use the existing `mark_read` (its own transaction).

## Checklist

### Phase 1: Open and read a doc

- Create `web/doc_view.py` with the `ROW_SQL` lookup and the `breadcrumbs(row)` helper (reusing `inbox.humanise_type`; Tracker / archived cases per decision 5).
- Implement `doc_shell`: 503 if db unconfigured, lookup by id, 404 if absent, build crumbs, set `available` from status, call `mark_read` after the read, render `doc.html`.
- Implement `doc_raw`: 404 for unknown / wrong-status / no-path, prefer `content_html`, else read `source_path` (404 on `OSError`), return `HTMLResponse`.
- Create `web/templates/doc.html` as a complete standalone document with its own `<style>`: fixed-height top bar (breadcrumbs + "View source file" link, shown only when available); full-height (`calc(100vh - bar)`) internal-scroll iframe (no sandbox) for available docs; "no longer available" variant otherwise; no sibling markup yet.
- Register `/doc/{document_id:int}` and `/doc/{document_id:int}/raw` in `web/app.py`.
- Update `web/templates/index.html`: wrap cards with a `document_id` in an `aria-label`'d anchor to `/doc/{id}`; leave feature/shipped cards plain; add `.card-link` + `:focus-visible` CSS.
- Write `web/doc_view_test.py`: shell 200 + breadcrumb + iframe src; unread cleared after shell view; raw GET does *not* stamp read-state; unknown/non-numeric → 404; raw serves file + 404 cases; missing-file unavailable (re-walk via a 2nd `POST /admin/discover`); tracker crumb; archived label; 503; index card links + aria-label.
- Run QC (pytest, ruff format, ruff check, ty check) clean; open one MR `feat(doc-view): phase 1 — render a doc and mark it read`.

### Phase 2: Sibling-doc navigation

- Add `DOC_TYPE_ORDER` to `storage/inbox.py` beside `_TYPE_LABELS` as the single ordering source.
- Add typed `siblings(conn, feature_id, current_id)` (id tie-break; returns `(None, None)` when `current_id` isn't in the active set) to `web/doc_view.py`; call it from `doc_shell` only for active feature docs; pass prev/next (id + humanised type) to the template.
- Render prev/next as `/doc/{id}` links in `doc.html`'s top bar, omitting whichever neighbour is absent; tracker docs show none.
- Extend `web/doc_view_test.py`: prev/next correct for a 3-doc feature; first has no prev, last no next; order independent of insertion; tracker has no nav.
- Run QC clean; open one MR `feat(doc-view): phase 2 — sibling-doc navigation`.
