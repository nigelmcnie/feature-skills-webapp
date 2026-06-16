"""Pure section parser and per-type manifests for feature docs.

No DB dependency — safe to import and unit-test without any schema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

# HTML5 void elements — never have closing tags.
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


@dataclass(frozen=True)
class Section:
    key: str  # the section's id attribute (empty string for opaque docs)
    body: str  # inner HTML, re-serialised deterministically


@dataclass(frozen=True)
class ParsedContent:
    shape: Literal["sections", "opaque"]
    sections: tuple[Section, ...]
    # shape="sections" with empty sections tuple is the sentinel for "no <main> found"
    # or "zero sections" — the importer logs these but doesn't abort.


@dataclass(frozen=True)
class ManifestSpec:
    shape: Literal["sections", "opaque"]
    section_labels: tuple[tuple[str, str], ...] = ()  # ordered (key, label) pairs
    repeated_prefixes: tuple[str, ...] = ()

    @property
    def expected_keys(self) -> tuple[str, ...]:
        return tuple(k for k, _ in self.section_labels)


# Manifests for the three section-parsed doc types.
_MANIFESTS: dict[str, ManifestSpec] = {
    "context": ManifestSpec(
        shape="sections",
        section_labels=(
            ("problem-space", "Problem space"),
            ("related-work", "Related work"),
            ("constraints", "Constraints"),
            ("links", "Links"),
            ("open-questions", "Open questions"),
        ),
    ),
    "requirements": ManifestSpec(
        shape="sections",
        section_labels=(
            ("problem", "Problem"),
            ("scope", "Scope"),
            ("vision", "Vision"),
            ("non-goals", "Non-goals"),
            ("user-stories", "User stories"),
            ("categories", "Categories"),
            ("data-model", "Data model"),
            ("technical-approach", "Technical approach"),
            ("testing", "Testing"),
            ("alternatives", "Alternatives"),
            ("delivery-phases", "Delivery phases"),
            ("indicative-notes", "Indicative notes"),
            ("design-notes", "Design notes"),
            ("review-decisions", "Review decisions"),
        ),
    ),
    "plan": ManifestSpec(
        shape="sections",
        section_labels=(
            ("overview", "Overview"),
            ("key-decisions", "Key technical decisions"),
            ("data-model", "Data model"),
            ("contract", "HTTP contract"),
            ("file-structure", "File structure"),
            ("qc", "QC"),
            ("checklist", "Checklist"),
        ),
        repeated_prefixes=("phase-",),
    ),
}


def manifest_for(doc_type: str) -> ManifestSpec:
    """Return the ManifestSpec for a doc_type.

    Opaque for 'features', any '*-feedback' type, and any unrecognised type.
    """
    if doc_type == "features" or doc_type.endswith("-feedback"):
        return ManifestSpec(shape="opaque")
    return _MANIFESTS.get(doc_type, ManifestSpec(shape="opaque"))


def humanise_section_key(key: str, labels: dict[str, str]) -> str:
    """Human label for a section key.

    The manifest label if known, else a prettified key in the manifest's own
    sentence-case convention ("Open questions", not "Open Questions") — the single
    source of truth so the inbox card and the diff heading can't drift apart.
    """
    if key in labels:
        return labels[key]
    return key.replace("-", " ").replace("_", " ").capitalize()


class _SectionParser(HTMLParser):
    """Extract direct <section id="..."> children of <main class="document">.

    Nested <section> elements stay part of their enclosing section's body.
    Header and chrome outside <main> are never captured.
    Uses convert_charrefs=False so entity/char refs round-trip faithfully.

    Section boundaries are tracked by counting only <section> nesting, not all
    tags. This makes boundary detection immune to unescaped HTML-like text in
    <pre>/<code> blocks (e.g. a code comment containing "<whole body>" would be
    parsed as an open tag by html.parser, corrupting a simple depth counter).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.found_main: bool = False
        self.sections: list[Section] = []

        self._in_main: bool = False
        # Tracks depth of non-section open elements directly inside <main>.
        # 0 = we're at the top level of <main> (ready to start a new section).
        # Only used before the first section (e.g. for <header>); void elements
        # are excluded so an <img> or <br> in the header doesn't corrupt it.
        self._main_child_depth: int = 0
        self._in_section: bool = False
        self._section_key: str = ""
        # Counts only nested <section> opens inside the current section.
        # Other tags are re-emitted verbatim without depth tracking so that
        # unknown/mismatched tags in code blocks don't affect section boundaries.
        self._nested_section_count: int = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._in_main:
            if tag == "main":
                attr_dict = dict(attrs)
                classes = set((attr_dict.get("class") or "").split())
                if "document" in classes:
                    self._in_main = True
                    self.found_main = True
            return

        if self._in_section:
            self._buf.append(self.get_starttag_text() or "")
            if tag == "section":
                self._nested_section_count += 1
        elif self._main_child_depth == 0 and tag == "section":
            attr_dict = dict(attrs)
            self._in_section = True
            self._section_key = attr_dict.get("id") or ""
            self._nested_section_count = 0
            self._buf = []
        elif tag not in _VOID_TAGS:
            self._main_child_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # XHTML-style self-closing syntax (e.g. <br/>). Call handle_starttag only —
        # the default implementation also calls handle_endtag which would falsely
        # close the enclosing section.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_main:
            return

        if self._in_section:
            if tag == "section":
                if self._nested_section_count > 0:
                    self._buf.append(f"</{tag}>")
                    self._nested_section_count -= 1
                else:
                    # Closing our top-level section — commit the body.
                    self.sections.append(Section(key=self._section_key, body="".join(self._buf)))
                    self._in_section = False
                    self._section_key = ""
                    self._buf = []
            else:
                self._buf.append(f"</{tag}>")
        else:
            if self._main_child_depth > 0:
                self._main_child_depth -= 1
            else:
                # Closing </main>.
                self._in_main = False

    def handle_data(self, data: str) -> None:
        if self._in_section:
            self._buf.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._in_section:
            self._buf.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._in_section:
            self._buf.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if self._in_section:
            self._buf.append(f"<!--{data}-->")


def parse_content(html: str, spec: ManifestSpec) -> ParsedContent:
    """Parse doc HTML into a ParsedContent according to spec.

    Opaque docs: returns a single Section(key="", body=html).
    Section docs: extracts direct <section id="..."> children of <main class="document">.
      - No <main> found, or zero sections: returns ParsedContent(shape="sections", sections=())
        as a sentinel the importer can log and skip.
    """
    if spec.shape == "opaque":
        return ParsedContent(shape="opaque", sections=(Section(key="", body=html),))

    parser = _SectionParser()
    parser.feed(html)

    if not parser.found_main or not parser.sections:
        return ParsedContent(shape="sections", sections=())

    return ParsedContent(shape="sections", sections=tuple(parser.sections))


def serialise(content: ParsedContent) -> str:
    """Compact JSON — both the stored representation and the byte-equality key.

    Deterministic: identical input bytes always produce identical output.
    Not semantic normalisation — attribute order and whitespace are preserved
    as authored (via get_starttag_text()), so a resave that only reorders
    attributes would cut a harmless spurious version.
    """
    return json.dumps(
        {
            "shape": content.shape,
            "sections": [{"key": s.key, "body": s.body} for s in content.sections],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
