"""Pure section/text diff for feature docs.

No DB dependency — safe to import and unit-test without any schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from feature_skills_webapp.storage.doc_content import ParsedContent

SectionStatus = Literal["added", "removed", "changed", "unchanged"]


@dataclass(frozen=True)
class SectionDiff:
    key: str
    status: SectionStatus


@dataclass(frozen=True)
class DocDiff:
    sections: tuple[SectionDiff, ...]  # in current order, removed appended

    @property
    def changed_keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.sections if s.status in ("added", "removed", "changed"))

    @property
    def changed_count(self) -> int:
        return len(self.changed_keys)

    @property
    def has_textual_change(self) -> bool:
        return self.changed_count > 0


class _TextExtractor(HTMLParser):
    """Collect character data, dropping all tags and HTML comments."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_comment(self, data: str) -> None:
        pass  # drop comments


def extract_text(html: str) -> str:
    """Tag-stripped, whitespace-collapsed text of a section body.

    Drops HTML comments; collapses all whitespace runs including newlines so
    that two bodies differing only in markup/whitespace/comments compare equal.
    """
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join("".join(parser._parts).split())


def diff_contents(prior: ParsedContent, current: ParsedContent) -> DocDiff:
    """Section-aware diff. A section is 'changed' when extract_text differs.

    Sections are matched by key; a renamed key reads as one removed + one added.
    Sections in current appear first (in current order); removed sections are
    appended at the end.
    """
    prior_texts = {s.key: extract_text(s.body) for s in prior.sections}
    current_texts = {s.key: extract_text(s.body) for s in current.sections}

    diffs: list[SectionDiff] = []
    for key in current_texts:
        if key not in prior_texts:
            diffs.append(SectionDiff(key=key, status="added"))
        elif current_texts[key] != prior_texts[key]:
            diffs.append(SectionDiff(key=key, status="changed"))
        else:
            diffs.append(SectionDiff(key=key, status="unchanged"))

    for key in prior_texts:
        if key not in current_texts:
            diffs.append(SectionDiff(key=key, status="removed"))

    return DocDiff(sections=tuple(diffs))
