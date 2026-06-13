"""Pure render module for feature docs.

No DB dependency — safe to import and unit-test without any schema.
"""

from __future__ import annotations

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
