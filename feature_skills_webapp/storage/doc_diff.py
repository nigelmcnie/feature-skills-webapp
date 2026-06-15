"""Pure section/text diff for feature docs.

No DB dependency — safe to import and unit-test without any schema.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from feature_skills_webapp.storage.doc_content import ParsedContent

SectionStatus = Literal["added", "removed", "changed", "unchanged"]
SegmentKind = Literal["equal", "insert", "delete"]


@dataclass(frozen=True)
class DiffSegment:
    kind: SegmentKind
    text: str  # plain text; may contain literal HTML characters that need escaping


@dataclass(frozen=True)
class SectionDiff:
    key: str
    status: SectionStatus
    segments: tuple[DiffSegment, ...] = ()  # word-level; non-empty only for "changed"
    current_body: str = ""  # raw HTML body; set for added/changed/unchanged
    prior_body: str = ""  # raw HTML body; set for removed


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


def _word_diff(prior_text: str, current_text: str) -> tuple[DiffSegment, ...]:
    """Compute word-level diff segments between two extracted texts."""
    prior_words = prior_text.split()
    current_words = current_text.split()
    matcher = difflib.SequenceMatcher(None, prior_words, current_words, autojunk=False)
    segments: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            segments.append(DiffSegment(kind="equal", text=" ".join(prior_words[i1:i2])))
        elif tag == "insert":
            segments.append(DiffSegment(kind="insert", text=" ".join(current_words[j1:j2])))
        elif tag == "delete":
            segments.append(DiffSegment(kind="delete", text=" ".join(prior_words[i1:i2])))
        elif tag == "replace":
            segments.append(DiffSegment(kind="delete", text=" ".join(prior_words[i1:i2])))
            segments.append(DiffSegment(kind="insert", text=" ".join(current_words[j1:j2])))
    return tuple(segments)


def diff_contents(prior: ParsedContent, current: ParsedContent) -> DocDiff:
    """Section-aware diff. A section is 'changed' when extract_text differs.

    Sections are matched by key; a renamed key reads as one removed + one added.
    Sections in current appear first (in current order); removed sections are
    appended at the end. Changed sections carry word-level DiffSegments.
    """
    prior_bodies = {s.key: s.body for s in prior.sections}
    current_bodies = {s.key: s.body for s in current.sections}
    prior_texts = {k: extract_text(v) for k, v in prior_bodies.items()}
    current_texts = {k: extract_text(v) for k, v in current_bodies.items()}

    diffs: list[SectionDiff] = []
    for key in current_texts:
        if key not in prior_texts:
            diffs.append(SectionDiff(key=key, status="added", current_body=current_bodies[key]))
        elif current_texts[key] != prior_texts[key]:
            segments = _word_diff(prior_texts[key], current_texts[key])
            diffs.append(
                SectionDiff(
                    key=key,
                    status="changed",
                    segments=segments,
                    current_body=current_bodies[key],
                )
            )
        else:
            diffs.append(SectionDiff(key=key, status="unchanged", current_body=current_bodies[key]))

    for key in prior_texts:
        if key not in current_texts:
            diffs.append(SectionDiff(key=key, status="removed", prior_body=prior_bodies[key]))

    return DocDiff(sections=tuple(diffs))
