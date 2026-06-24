"""Pure render module for feature docs.

No DB dependency — safe to import and unit-test without any schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

import tinycss2
from markupsafe import Markup, escape

from feature_skills_webapp.storage.doc_content import (
    ManifestSpec,
    ParsedContent,
    humanise_section_key,
)
from feature_skills_webapp.storage.doc_diff import DiffSegment, DocDiff


def render_section_doc(content: ParsedContent, manifest: ManifestSpec) -> Markup:
    """Return inner HTML for <main>: each stored section wrapped in <section id="{key}">.

    Sections are ordered by the manifest's section_labels; keys not in the manifest
    are rendered last in stored order. Section bodies are already inner content (they
    carry their own <h2>). Returns markupsafe.Markup so autoescape passes it through.
    """
    sections_by_key = {s.key: s for s in content.sections}
    manifest_keys = [k for k, _ in manifest.section_labels]

    ordered: list[str] = []
    seen: set[str] = set()

    # Manifest-ordered first
    for key in manifest_keys:
        if key in sections_by_key:
            ordered.append(key)
            seen.add(key)

    # Remaining stored keys not in manifest
    for s in content.sections:
        if s.key not in seen:
            ordered.append(s.key)

    parts: list[str] = []
    for key in ordered:
        section = sections_by_key[key]
        parts.append(f'<section id="{key}">{section.body}</section>')

    return Markup("".join(parts))


def _render_diff_segments(segments: tuple[DiffSegment, ...]) -> str:
    """Render word-level diff segments as HTML with escaped text in ins/del."""
    pieces: list[str] = []
    for seg in segments:
        text = str(escape(seg.text))
        if seg.kind == "equal":
            pieces.append(text)
        elif seg.kind == "insert":
            pieces.append(f"<ins>{text}</ins>")
        elif seg.kind == "delete":
            pieces.append(f"<del>{text}</del>")
    return " ".join(pieces)


def render_diff(doc_diff: DocDiff, manifest: ManifestSpec) -> Markup:
    """Return inner HTML for <main> showing a section-level and word-level diff.

    Changed sections render their word-level segments with ins/del tags.
    Added/removed sections render their full body HTML with a status class.
    Unchanged sections render their full body HTML with a muted class.
    Section order follows the manifest (as in render_section_doc).
    """
    sections_by_key = {s.key: s for s in doc_diff.sections}
    manifest_keys = [k for k, _ in manifest.section_labels]
    label_map = dict(manifest.section_labels)

    ordered: list[str] = []
    seen: set[str] = set()
    for key in manifest_keys:
        if key in sections_by_key:
            ordered.append(key)
            seen.add(key)
    for sd in doc_diff.sections:
        if sd.key not in seen:
            ordered.append(sd.key)

    parts: list[str] = []
    for key in ordered:
        sd = sections_by_key[key]
        safe_key = str(escape(key))
        if sd.status == "unchanged":
            parts.append(
                f'<section id="{safe_key}" class="diff-unchanged">{sd.current_body}</section>'
            )
        elif sd.status == "added":
            parts.append(f'<section id="{safe_key}" class="diff-added">{sd.current_body}</section>')
        elif sd.status == "removed":
            parts.append(f'<section id="{safe_key}" class="diff-removed">{sd.prior_body}</section>')
        elif sd.status == "changed":
            label = str(escape(humanise_section_key(key, label_map)))
            seg_html = _render_diff_segments(sd.segments)
            parts.append(
                f'<section id="{safe_key}" class="diff-changed">'
                f"<h2>{label}</h2>"
                f'<div class="diff-text">{seg_html}</div>'
                f"</section>"
            )

    return Markup("".join(parts))


class _SafeInnerParser(HTMLParser):
    """Extract inner content of <main class="document"> or <body> as fallback.

    Uses two buffers: _main_buf for <main class="document"> inner content,
    _body_buf for <body> inner content. Prefers _main_buf if main was found.
    Strips <script>, <style>, and <head> tags and their content.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._in_body: bool = False
        self._in_main: bool = False  # inside <main class="document">
        self._found_main: bool = False
        self._skip_tag: str | None = None  # tag being skipped with its subtree
        self._skip_depth: int = 0
        self._main_buf: list[str] = []
        self._body_buf: list[str] = []

    def _active_buf(self) -> list[str] | None:
        if self._skip_tag is not None:
            return None
        if self._in_main:
            return self._main_buf
        if self._in_body and not self._found_main:
            return self._body_buf
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return

        if tag in ("script", "style", "head"):
            self._skip_tag = tag
            self._skip_depth = 1
            return

        if not self._in_body:
            if tag == "body":
                self._in_body = True
            return

        if not self._in_main and tag == "main":
            attr_dict = dict(attrs)
            classes = set((attr_dict.get("class") or "").split())
            if "document" in classes:
                self._in_main = True
                self._found_main = True
                return  # don't emit the <main> tag itself

        buf = self._active_buf()
        if buf is not None:
            buf.append(self.get_starttag_text() or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return

        if self._in_main and tag == "main":
            self._in_main = False
            return  # don't emit </main>

        if self._in_body and tag == "body":
            self._in_body = False
            return

        buf = self._active_buf()
        if buf is not None:
            buf.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        buf = self._active_buf()
        if buf is not None:
            buf.append(data)

    def handle_entityref(self, name: str) -> None:
        buf = self._active_buf()
        if buf is not None:
            buf.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        buf = self._active_buf()
        if buf is not None:
            buf.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        buf = self._active_buf()
        if buf is not None:
            buf.append(f"<!--{data}-->")

    def result(self) -> str:
        return "".join(self._main_buf if self._found_main else self._body_buf)


def extract_safe_inner(html: str) -> Markup:
    """Return the inner HTML of <main class="document"> if present, else <body>.

    Strips <script>, <style>, and <head> elements (accident-prevention for trusted
    self-authored corpus). Inline event-handler attributes and javascript: URLs are
    NOT scrubbed — documented, not an oversight. Returns markupsafe.Markup.
    """
    parser = _SafeInnerParser()
    parser.feed(html)
    return Markup(parser.result())


# Unscopable document-level CSS at-rules that must be dropped before @scope wrapping.
_UNSCOPABLE_AT_RULE_RE = re.compile(r"@(import|charset|namespace)\b[^;]*;", re.IGNORECASE)


def css_has_brace_error(css: str) -> bool:
    """True if the CSS has a stray/unmatched closing brace.

    Such a brace would let author CSS break out of an enclosing
    ``@scope (#doc-main) { ... }`` block and bleed into the shell chrome.
    Uses tinycss2's component-value tokeniser, so braces inside strings or
    comments (e.g. ``content: "}"``) are correctly ignored and an unclosed
    block (which cannot break *out*) is tolerated — only a stray ``}`` at the
    top level surfaces as an error node.
    """
    return any(node.type == "error" for node in tinycss2.parse_component_value_list(css))


def _drop_css_brace_errors(css: str) -> str:
    """Re-serialise the CSS with stray-brace (error) nodes removed.

    Best-effort sanitiser for CSS we cannot reject at a write boundary (the
    gathered ``<style>`` of an already-stored opaque doc): dropping the stray
    ``}`` token keeps the remaining rules but prevents the break-out, so what
    survives stays inside the ``@scope`` block.
    """
    nodes = tinycss2.parse_component_value_list(css)
    if not any(node.type == "error" for node in nodes):
        return css
    return "".join(node.serialize() for node in nodes if node.type != "error")


class _StyleCapturingInnerParser(_SafeInnerParser):
    """Like _SafeInnerParser but captures <style> text instead of discarding it.

    <script> and <head> are still stripped. <style> elements are collected into
    _style_buf; their tags are NOT emitted into the main body buffer.
    """

    def __init__(self) -> None:
        super().__init__()
        self._in_style: bool = False
        self._style_depth: int = 0
        self._style_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Intercept <style> before the parent skip logic runs.
        if self._skip_tag is None and tag == "style":
            self._in_style = True
            self._style_depth = 1
            return
        if self._in_style:
            self._style_depth += 1
            return
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._in_style:
            if tag == "style":
                self._style_depth -= 1
                if self._style_depth == 0:
                    self._in_style = False
            return
        super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._style_buf.append(data)
            return
        super().handle_data(data)

    def gathered_css(self) -> str:
        return "".join(self._style_buf)


def _neutralise_css(css: str) -> str:
    """Prevent author CSS from breaking out of a <style> element.

    Replaces literal </style> (case-insensitive) and <!-- with harmless
    stand-ins so they can't terminate the enclosing <style> tag or open an
    HTML comment that swallows subsequent content.
    """
    css = re.sub(r"</style>", r"<\\/style>", css, flags=re.IGNORECASE)
    css = css.replace("<!--", "<!\\-\\-")
    return css


def extract_safe_inner_with_css(html: str) -> tuple[Markup, str]:
    """Return (inner_html, author_css) for an opaque doc body.

    inner_html: inner content of <main class="document"> or <body>, with
      <script>/<head> stripped and <style> elements removed from the body
      (their text is captured separately).
    author_css: gathered <style> text with unscopable at-rules (@import,
      @charset, @namespace) dropped, and </style>/<!-- neutralised so the
      CSS is safe to re-emit inside a <style> element.
    """
    parser = _StyleCapturingInnerParser()
    parser.feed(html)
    raw_css = parser.gathered_css()
    # Drop @import, @charset, @namespace before scoping.
    cleaned = _UNSCOPABLE_AT_RULE_RE.sub("", raw_css).strip()
    # Drop stray closing braces so a malformed legacy <style> can't break out
    # of the @scope block (we can't reject it — the doc is already stored).
    cleaned = _drop_css_brace_errors(cleaned)
    return Markup(parser.result()), _neutralise_css(cleaned)


@dataclass(frozen=True)
class FeedbackItem:
    item_num: int
    tier: str  # "needs-input" | "feedback" | "routine"
    title_html: str  # inner HTML of <h3> for articles, or <span class="body"> for routine
    detail_html: str  # inner HTML of .detail (empty for routine)
    my_take_html: str  # inner HTML of .my-take (empty for routine)
    kind: str  # "response" | "routine"


class _FeedbackParser(HTMLParser):
    """Extract FeedbackItems from a feedback doc's HTML.

    Recognises tier sections (tier-needs-input, tier-feedback, tier-routine),
    article.item elements, and li.routine-item elements.

    Boundaries are tracked by counting only the *relevant* element's own tag
    nesting — the tier by <section>, the item by <article>/<li>, each captured
    field by its own wrapper tag — never a single global tag depth. Authored
    feedback bodies routinely carry imbalanced inline markup (a stray </p>, a
    <div> a browser would auto-close); a global depth counter desyncs on the
    first such imbalance and then silently drops every later sibling item in the
    tier. Counting per-element makes boundary detection immune to body markup,
    mirroring _SectionParser in doc_content.py.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._tier: str | None = None
        self._section_depth = 0  # <section> nesting since entering the tier
        self._item_num: int | None = None
        self._item_kind: str | None = None  # "response" | "routine"
        self._item_tag: str | None = None  # "article" | "li" — bounds the item
        self._item_depth = 0
        self._capture: str | None = None  # "h3" | "detail" | "my-take" | "body-span"
        self._capture_tag: str | None = None  # wrapper tag whose nesting bounds the capture
        self._capture_depth = 0
        self._buf: list[str] = []
        self._title_html = ""
        self._detail_html = ""
        self._my_take_html = ""
        self._skip_tag: str | None = None  # script/style/head subtree being dropped
        self._skip_depth = 0
        self.items: list[FeedbackItem] = []

    def _begin_capture(self, field: str, tag: str) -> None:
        self._capture = field
        self._capture_tag = tag
        self._capture_depth = 1  # the wrapper's own start tag
        self._buf = []

    def _flush_capture(self) -> None:
        captured = "".join(self._buf).strip()
        if self._capture == "h3":
            self._title_html = captured
        elif self._capture == "detail":
            self._detail_html = captured
        elif self._capture == "my-take":
            self._my_take_html = captured
        elif self._capture == "body-span":
            self._title_html = captured
        self._capture = None
        self._capture_tag = None
        self._capture_depth = 0
        self._buf = []

    def _open_item(self, attr_dict: dict[str, str | None], tag: str, kind: str) -> None:
        data_item = attr_dict.get("data-item")
        if data_item and data_item.isdigit():
            self._item_num = int(data_item)
            self._item_kind = kind
            self._item_tag = tag
            self._item_depth = 1  # the item element's own start tag

    def _flush_item(self) -> None:
        if self._item_num is None:
            return
        self.items.append(
            FeedbackItem(
                item_num=self._item_num,
                tier=self._tier or "",
                title_html=self._title_html,
                detail_html=self._detail_html,
                my_take_html=self._my_take_html,
                kind=self._item_kind or "response",
            )
        )
        self._item_num = None
        self._item_kind = None
        self._item_tag = None
        self._item_depth = 0
        self._title_html = ""
        self._detail_html = ""
        self._my_take_html = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return

        if tag in ("script", "style", "head"):
            self._skip_tag = tag
            self._skip_depth = 1
            return

        if self._capture is not None:
            self._buf.append(self.get_starttag_text() or "")
            if tag == self._capture_tag:
                self._capture_depth += 1
            return

        attr_dict = dict(attrs)
        classes = set((attr_dict.get("class") or "").split())

        if self._item_num is not None:
            # Inside an item: start capturing a known field, else just track item nesting.
            if self._item_kind == "response":
                if tag == "h3" and not self._title_html:
                    self._begin_capture("h3", tag)
                    return
                if tag == "div" and "detail" in classes:
                    self._begin_capture("detail", tag)
                    return
                if tag == "div" and "my-take" in classes:
                    self._begin_capture("my-take", tag)
                    return
            elif self._item_kind == "routine" and tag == "span" and "body" in classes:
                self._begin_capture("body-span", tag)
                return
            if tag == self._item_tag:
                self._item_depth += 1
            return

        if self._tier is not None:
            if tag == "section":
                self._section_depth += 1
            elif self._tier != "routine" and tag == "article" and "item" in classes:
                self._open_item(attr_dict, "article", "response")
            elif self._tier == "routine" and tag == "li" and "routine-item" in classes:
                self._open_item(attr_dict, "li", "routine")
            return

        if tag == "section" and "tier" in classes:
            for cls in classes:
                if cls.startswith("tier-"):
                    self._tier = cls[len("tier-") :]
                    self._section_depth = 1
                    break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            return
        if self._capture is not None:
            self._buf.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return

        if self._capture is not None:
            if tag == self._capture_tag:
                self._capture_depth -= 1
                if self._capture_depth == 0:
                    self._flush_capture()  # wrapper closed; inner HTML committed
                    return
            self._buf.append(f"</{tag}>")
            return

        if self._item_num is not None:
            if tag == self._item_tag:
                self._item_depth -= 1
                if self._item_depth == 0:
                    self._flush_item()
            return

        if self._tier is not None and tag == "section":
            self._section_depth -= 1
            if self._section_depth == 0:
                self._tier = None

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._capture is not None:
            self._buf.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._capture is not None:
            self._buf.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(f"<!--{data}-->")


def parse_feedback_items(html: str) -> list[FeedbackItem]:
    """Parse a feedback doc's HTML into FeedbackItems, sorted by item_num.

    Returns an empty list for malformed or featureless HTML.
    """
    parser = _FeedbackParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    return sorted(parser.items, key=lambda it: it.item_num)
