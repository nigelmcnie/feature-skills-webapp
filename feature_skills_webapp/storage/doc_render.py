"""Pure render module for feature docs.

No DB dependency — safe to import and unit-test without any schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

from markupsafe import Markup

from feature_skills_webapp.storage.doc_content import ManifestSpec, ParsedContent


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


@dataclass(frozen=True)
class FeedbackItem:
    item_num: int
    tier: str  # "needs-input" | "feedback" | "routine"
    title_html: str  # inner HTML of <h3> for articles, or <span class="body"> for routine
    detail_html: str  # inner HTML of .detail (empty for routine)
    my_take_html: str  # inner HTML of .my-take (empty for routine)
    kind: str  # "response" | "routine"


# HTML5 void elements — no matching end tag, so excluded from depth tracking.
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _FeedbackParser(HTMLParser):
    """Extract FeedbackItems from a feedback doc's HTML.

    Recognises tier sections (tier-needs-input, tier-feedback, tier-routine),
    article.item elements, and li.routine-item elements. Uses depth tracking
    with void-element exclusion so nested HTML content is captured correctly.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._depth = 0
        self._tier: str | None = None
        self._tier_entry_depth = 0
        self._item_num: int | None = None
        self._item_kind: str | None = None  # "response" | "routine"
        self._item_entry_depth = 0
        self._capture: str | None = None  # "h3" | "detail" | "my-take" | "body-span"
        self._capture_entry_depth = 0
        self._buf: list[str] = []
        self._title_html = ""
        self._detail_html = ""
        self._my_take_html = ""
        self._skip_tag: str | None = None
        self._skip_entry_depth = 0
        self.items: list[FeedbackItem] = []

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
        self._buf = []

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
        self._title_html = ""
        self._detail_html = ""
        self._my_take_html = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag not in _VOID_TAGS:
                self._depth += 1
            return

        if tag in ("script", "style", "head"):
            self._depth += 1
            self._skip_tag = tag
            self._skip_entry_depth = self._depth
            return

        if tag not in _VOID_TAGS:
            self._depth += 1

        if self._capture is not None:
            self._buf.append(self.get_starttag_text() or "")
            return

        attr_dict = dict(attrs)
        classes = set((attr_dict.get("class") or "").split())

        if self._item_num is not None:
            if self._item_kind == "response":
                if tag == "h3" and not self._title_html:
                    self._capture = "h3"
                    self._capture_entry_depth = self._depth
                elif tag == "div" and "detail" in classes:
                    self._capture = "detail"
                    self._capture_entry_depth = self._depth
                elif tag == "div" and "my-take" in classes:
                    self._capture = "my-take"
                    self._capture_entry_depth = self._depth
            elif self._item_kind == "routine" and tag == "span" and "body" in classes:
                self._capture = "body-span"
                self._capture_entry_depth = self._depth
            return

        if self._tier is not None:
            if self._tier != "routine" and tag == "article" and "item" in classes:
                data_item = attr_dict.get("data-item")
                if data_item and data_item.isdigit():
                    self._item_num = int(data_item)
                    self._item_kind = "response"
                    self._item_entry_depth = self._depth
            elif self._tier == "routine" and tag == "li" and "routine-item" in classes:
                data_item = attr_dict.get("data-item")
                if data_item and data_item.isdigit():
                    self._item_num = int(data_item)
                    self._item_kind = "routine"
                    self._item_entry_depth = self._depth
            return

        if tag == "section" and "tier" in classes:
            for cls in classes:
                if cls.startswith("tier-"):
                    self._tier = cls[len("tier-") :]
                    self._tier_entry_depth = self._depth
                    break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            return
        if self._capture is not None:
            self._buf.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag not in _VOID_TAGS:
                self._depth -= 1
                if tag == self._skip_tag and self._depth < self._skip_entry_depth:
                    self._skip_tag = None
            return

        if tag in _VOID_TAGS:
            return

        if self._capture is not None:
            if self._depth == self._capture_entry_depth:
                self._flush_capture()
                self._depth -= 1
                return
            self._buf.append(f"</{tag}>")
            self._depth -= 1
            return

        if self._item_num is not None and self._depth == self._item_entry_depth:
            self._flush_item()
            self._depth -= 1
            return

        if self._tier is not None and self._depth == self._tier_entry_depth:
            self._tier = None
            self._depth -= 1
            return

        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_tag is not None:
            return
        if self._capture is not None:
            self._buf.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._skip_tag is not None:
            return
        if self._capture is not None:
            self._buf.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skip_tag is not None:
            return
        if self._capture is not None:
            self._buf.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if self._skip_tag is not None:
            return
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
