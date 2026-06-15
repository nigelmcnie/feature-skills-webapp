"""Unit tests for storage/doc_diff.py."""

from __future__ import annotations

from feature_skills_webapp.storage.doc_content import ParsedContent, Section
from feature_skills_webapp.storage.doc_diff import (
    SectionDiff,
    diff_contents,
    extract_text,
)

# --- extract_text ---


def test_extract_text_strips_tags() -> None:
    assert extract_text("<p>hello</p>") == "hello"


def test_extract_text_collapses_interior_whitespace() -> None:
    assert extract_text("<p>  hello   world  </p>") == "hello world"


def test_extract_text_collapses_newlines() -> None:
    assert extract_text("<p>hello\n  world</p>") == "hello world"


def test_extract_text_drops_html_comments() -> None:
    assert extract_text("<p><!-- a note -->hello</p>") == "hello"


def test_extract_text_markup_only_change_produces_equal_text() -> None:
    a = "<p>hello world</p>"
    b = "<div class='x'><span>hello</span> world</div>"
    assert extract_text(a) == extract_text(b)


def test_extract_text_whitespace_only_change_produces_equal_text() -> None:
    assert extract_text("<p>hello world</p>") == extract_text("<p>hello    world</p>")


def test_extract_text_comment_only_change_produces_equal_text() -> None:
    assert extract_text("<p>hello</p>") == extract_text("<p><!-- note -->hello</p>")


def test_extract_text_empty_html() -> None:
    assert extract_text("") == ""


def test_extract_text_only_tags() -> None:
    assert extract_text("<p></p><br/>") == ""


# --- diff_contents helpers ---


def _content(*sections: tuple[str, str]) -> ParsedContent:
    return ParsedContent(
        shape="sections",
        sections=tuple(Section(key=k, body=b) for k, b in sections),
    )


# --- diff_contents ---


def test_diff_unchanged_section() -> None:
    c = _content(("a", "<p>same</p>"))
    d = diff_contents(c, c)
    assert d.sections == (SectionDiff(key="a", status="unchanged"),)
    assert d.changed_count == 0
    assert not d.has_textual_change


def test_diff_changed_section() -> None:
    prior = _content(("a", "<p>old</p>"))
    current = _content(("a", "<p>new</p>"))
    d = diff_contents(prior, current)
    assert d.sections == (SectionDiff(key="a", status="changed"),)
    assert d.changed_count == 1
    assert d.has_textual_change


def test_diff_added_section() -> None:
    prior = _content(("a", "<p>same</p>"))
    current = _content(("a", "<p>same</p>"), ("b", "<p>new section</p>"))
    d = diff_contents(prior, current)
    assert SectionDiff(key="a", status="unchanged") in d.sections
    assert SectionDiff(key="b", status="added") in d.sections
    assert "b" in d.changed_keys
    assert d.changed_count == 1


def test_diff_removed_section() -> None:
    prior = _content(("a", "<p>same</p>"), ("b", "<p>old section</p>"))
    current = _content(("a", "<p>same</p>"))
    d = diff_contents(prior, current)
    assert SectionDiff(key="b", status="removed") in d.sections
    assert "b" in d.changed_keys
    assert d.changed_count == 1


def test_diff_reordered_sections_reads_as_unchanged() -> None:
    prior = _content(("a", "<p>alpha</p>"), ("b", "<p>beta</p>"))
    current = _content(("b", "<p>beta</p>"), ("a", "<p>alpha</p>"))
    d = diff_contents(prior, current)
    assert d.changed_count == 0


def test_diff_empty_sections_sentinel_no_exception() -> None:
    empty = ParsedContent(shape="sections", sections=())
    d = diff_contents(empty, empty)
    assert d.changed_count == 0


def test_diff_markup_only_change_reads_as_unchanged() -> None:
    prior = _content(("a", "<p>hello world</p>"))
    current = _content(("a", "<div>hello   world</div>"))
    d = diff_contents(prior, current)
    assert d.changed_count == 0


def test_diff_changed_keys_excludes_unchanged() -> None:
    prior = _content(("a", "<p>old</p>"), ("b", "<p>same</p>"))
    current = _content(("a", "<p>new</p>"), ("b", "<p>same</p>"))
    d = diff_contents(prior, current)
    assert d.changed_keys == ("a",)


def test_diff_removed_sections_appended_after_current() -> None:
    prior = _content(("a", "<p>a</p>"), ("b", "<p>b</p>"))
    current = _content(("a", "<p>a</p>"))
    d = diff_contents(prior, current)
    # current-order entries first, removed appended
    assert d.sections[0] == SectionDiff(key="a", status="unchanged")
    assert d.sections[-1] == SectionDiff(key="b", status="removed")


def test_diff_multiple_changes() -> None:
    prior = _content(("a", "<p>old</p>"), ("b", "<p>same</p>"), ("c", "<p>gone</p>"))
    current = _content(("a", "<p>new</p>"), ("b", "<p>same</p>"), ("d", "<p>fresh</p>"))
    d = diff_contents(prior, current)
    assert d.changed_count == 3  # a changed, c removed, d added
    assert set(d.changed_keys) == {"a", "c", "d"}
