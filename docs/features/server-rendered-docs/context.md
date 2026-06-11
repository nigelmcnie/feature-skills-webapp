# server-rendered-docs

## Problem space and motivation

Today a doc is rendered as a thin shell that iframes the standalone HTML file: `web/doc_view.py` `doc_shell` serves `doc.html`, which embeds `<iframe src="/doc/{id}/raw">`; `doc_raw` serves the doc's own self-contained HTML live from `source_path` (preferring `content_html` if present). Because the rendered doc is an opaque file inside a frame, the two interactive features have to reach *into* that frame to work. Synthesis reads `.your-thoughts textarea` and `.tier-routine .flag-btn` out of `frame.contentDocument`; comments read `window.__fsComments` out of `frame.contentWindow` — both via inline scripts in `doc.html`, POSTing to `/doc/{id}/synthesis-response` and `/doc/{id}/comments`.

That DOM-scraping only works because the feature-skills templates are rigid, and it's brittle by construction — a template tweak in the other repo can silently break the handoff (the `skill-webapp-integration` work hit exactly this class of bug). It's the fragility the A2 decision (structured content in the DB) was chosen to retire.

Once [versioned-content-store](../versioned-content-store/context.html) (F1) lands, the doc's content lives in the DB as structured sections — so the webapp can render the doc itself instead of iframing a file. This feature (F2) is that switch: **render docs server-side from the stored content, drop the iframe, and re-home comments and synthesis as native webapp UI** backed by the existing `comments` and `synthesis_responses` tables. The interaction stops being scraped through a frame and becomes first-class.

Crucially, the rendering switch and the native-interaction switch are *one* change, not two: dropping the iframe breaks the current scrape-the-frame interaction, so re-homing it natively isn't optional — it comes with the move. That's deliberate; the alternative is maintaining a halfway house of old-and-new side by side, which isn't worth it given how fast this is moving. For the same reason, all doc types switch together (context, requirements, plan, feedback/synthesis, and the tracker surfaces) rather than one at a time — doing them all at once is what surfaces the issues in any of them.

## Related work

- **[versioned-content-store](../versioned-content-store/context.html) (F1, the hard dependency).** Lands the structured content model (`{doc_type, ordered sections[{key, body}]}` + versions) and the importer. F2 is its first real consumer — F1 deliberately leaves rendering and interaction untouched precisely so F2 can switch them all at once. F2 should not start before F1's content is in the DB.
- **[doc-view](../doc-view/context.html) (shipped).** Built the very rendering path F2 replaces: the `/doc/{id}` shell, the `/doc/{id}/raw` passthrough, the no-sandbox full-height iframe, sibling prev/next nav, breadcrumbs, and the missing/archived/503 states. Its context records why the iframe-passthrough was chosen over inlining — and explicitly flagged inlining as a "Stage 2" follow-up. F2 is that follow-up.
- **[synthesis-response-capture](../synthesis-response-capture/requirements.html) (shipped).** Built `web/synthesis.py` and the `synthesis_responses` table, and chose to read the feedback iframe's DOM directly from the shell — a signed-off divergence at the time. F2 changes *how* the response is gathered (native widgets, not DOM scrape) while keeping the same storage and POST contract.
- **[skill-webapp-integration](../skill-webapp-integration/requirements.html) (shipped).** Built `web/comments.py`, the comment-rail handoff via `window.__fsComments`, and the HTTP read/integrate surface. The endpoints and `comments` table stay; the frame-scraping front-end is what F2 retires.
- **Other rendering surfaces.** `web/feature_page.py` and `web/project_page.py` already render server-side from the DB (no iframe), so they're the existing pattern F2 extends to the doc views. The project page also renders the tracker, which F1 ingests + versions.

## Constraints and considerations

**Hard dependency on F1.** F2 renders from stored structured content, so it can't land until `versioned-content-store` has populated the DB. The split is by pipeline stage: F1 = additive plumbing (iframe still renders from file); F2 = the rendering + interaction cutover.

**No halfway house.** All doc types switch together, and native comments/synthesis ship with the rendering switch — not as a later increment. This is an explicit decision, made on the grounds that maintaining old-and-new in parallel is a greater cost than just doing the whole switch.

**The presentation/interaction layer relocates repos.** Today each feature-skills template carries its own CSS/JS — TOC, syntax-ish highlighting, the comment-rail widget, the synthesis response widgets, the sticky footer CTA. When the webapp renders from structured content, that layer becomes the webapp's, owned once and applied to every doc, rather than copied into each template. This is a meaningful shift in where doc styling lives.

**The webapp owns the section manifest (decided), and this is now a cross-agent requirement.** A parallel goal has emerged — make this workflow usable from Codex as well as Claude (see the cross-agent plan in Links). Each template today fuses two things: the *section manifest* (which sections, in what order, with what labels — what the webapp renders and what any agent must fill) and the *authoring guidance* (what to write in each). For cross-agent use the manifest must be single-sourced, or Claude's skills and Codex's skills each carry a copy and drift. The decision: **the webapp owns the manifest** (it has to, to render) and the shared workflow spec; agent skills become thin wrappers that reference it. So F2's rendering work is also where the manifest finishes migrating out of the templates — which is what ultimately lets the agent wrappers be thin.

**The endpoints and tables stay; the front-end changes.** `/doc/{id}/synthesis-response`, `/doc/{id}/comments` and their tables (`synthesis_responses`, `comments`) are kept — F2 swaps the scrape-the-iframe front-end for native server-rendered widgets that post to the same contracts. The shell furniture from `doc-view` (breadcrumbs, sibling nav, archived/missing/503 states, action-bar CTAs) must survive the move to a single server-rendered page.

**This unblocks the headline feature.** The flagged-inbox + toggleable diff view (a separate, later feature) needs F1 (versions to diff) and F2 (a render path it can show a diff inside of). It does *not* need the skills cutover.

**Not in scope.** The MCP submission tool, the feature-skills skills cutover (skills still write files), and dev-store deletion are all later, separate features. F2 is webapp-side rendering only.

## Links

- Upstream dependency: [versioned-content-store (F1)](../versioned-content-store/context.html)
- Shell to replace: `feature_skills_webapp/web/doc_view.py`, `feature_skills_webapp/web/templates/doc.html`
- Endpoints + tables that stay: `web/synthesis.py`, `web/comments.py`
- Existing server-rendered surfaces: `web/feature_page.py`, `web/project_page.py`
- Templates carrying presentation/JS: `~/src/nigelmcnie/feature-skills/feature/{context,requirements,plan,feedback}-template.html`
- Precedent: [doc-view context](../doc-view/context.html) — built the iframe rendering this replaces (and flagged inlining as Stage 2).
- Cross-agent plan: [codex/plan.md](file:///home/nigel/codex/plan.md) — why F1/F2 are the path to dual Claude+Codex use.
- Downstream: [agent-submission-api](../agent-submission-api/context.html) — the post-F2 cross-agent write contract this enables.

## Open questions

1. **How is the webapp-owned presentation layer structured?** It's decided that the webapp owns one canonical doc stylesheet/template + the section manifest (rather than each feature-skills template carrying its own copy). Open: do the per-doc-type manifests from F1 drive layout/ordering declaratively, and how is the manifest exposed so a future Codex wrapper can fill sections without embedding any presentation knowledge?
2. **How is synthesis interaction modelled natively?** It's richer than comments — per-item textareas keyed by `item_num`, plus `tier-routine` flag buttons with their own flag text. Re-homing it means rendering those widgets from the feedback doc's structured content and posting to the existing `/synthesis-response` shape, with nothing about the feedback structure lost in translation.
3. **Is there still a "view source" / raw hatch?** `doc-view` kept a "View source file" link to the original HTML. Once content lives in the DB and is rendered natively, is the rendered view the only view, or do we keep a way to see the raw stored content (useful when something renders wrong)?
4. **Does F2 re-render the tracker surfaces too, or only per-doc views?** The tracker is ingested + versioned in F1 and surfaced on the project page (already server-rendered). Decide whether F2 re-points the project/tracker rendering at the stored content or leaves those surfaces as-is and only converts the per-doc views.
5. **What happens to docs that pre-date / fail F1's structured import?** If a doc has no structured content yet (or the importer couldn't parse it), does F2 fall back to the old raw view, show a "can't render" state, or is F1 guaranteed to have covered the whole corpus before F2 ships?
