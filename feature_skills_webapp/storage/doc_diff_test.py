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


def _status(sd: SectionDiff) -> tuple[str, str]:
    return (sd.key, sd.status)


def test_diff_unchanged_section() -> None:
    c = _content(("a", "<p>same</p>"))
    d = diff_contents(c, c)
    assert len(d.sections) == 1
    assert _status(d.sections[0]) == ("a", "unchanged")
    assert d.changed_count == 0
    assert not d.has_textual_change


def test_diff_changed_section() -> None:
    prior = _content(("a", "<p>old</p>"))
    current = _content(("a", "<p>new</p>"))
    d = diff_contents(prior, current)
    assert len(d.sections) == 1
    assert _status(d.sections[0]) == ("a", "changed")
    assert d.changed_count == 1
    assert d.has_textual_change


def test_diff_added_section() -> None:
    prior = _content(("a", "<p>same</p>"))
    current = _content(("a", "<p>same</p>"), ("b", "<p>new section</p>"))
    d = diff_contents(prior, current)
    statuses = {_status(s) for s in d.sections}
    assert ("a", "unchanged") in statuses
    assert ("b", "added") in statuses
    assert "b" in d.changed_keys
    assert d.changed_count == 1


def test_diff_removed_section() -> None:
    prior = _content(("a", "<p>same</p>"), ("b", "<p>old section</p>"))
    current = _content(("a", "<p>same</p>"))
    d = diff_contents(prior, current)
    statuses = {_status(s) for s in d.sections}
    assert ("b", "removed") in statuses
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
    assert _status(d.sections[0]) == ("a", "unchanged")
    assert _status(d.sections[-1]) == ("b", "removed")


def test_diff_multiple_changes() -> None:
    prior = _content(("a", "<p>old</p>"), ("b", "<p>same</p>"), ("c", "<p>gone</p>"))
    current = _content(("a", "<p>new</p>"), ("b", "<p>same</p>"), ("d", "<p>fresh</p>"))
    d = diff_contents(prior, current)
    assert d.changed_count == 3  # a changed, c removed, d added
    assert set(d.changed_keys) == {"a", "c", "d"}


# --- body fields on SectionDiff ---


def test_diff_unchanged_carries_current_body() -> None:
    c = _content(("a", "<p>hello</p>"))
    d = diff_contents(c, c)
    assert d.sections[0].current_body == "<p>hello</p>"
    assert d.sections[0].prior_body == ""


def test_diff_added_carries_current_body() -> None:
    prior = _content(("a", "<p>old</p>"))
    current = _content(("a", "<p>old</p>"), ("b", "<p>brand new</p>"))
    d = diff_contents(prior, current)
    added = next(s for s in d.sections if s.key == "b")
    assert added.current_body == "<p>brand new</p>"
    assert added.prior_body == ""


def test_diff_removed_carries_prior_body() -> None:
    prior = _content(("a", "<p>gone</p>"))
    current = _content()
    d = diff_contents(prior, current)
    removed = next(s for s in d.sections if s.key == "a")
    assert removed.prior_body == "<p>gone</p>"
    assert removed.current_body == ""


def test_diff_changed_carries_current_body_and_segments() -> None:
    prior = _content(("a", "<p>hello world</p>"))
    current = _content(("a", "<p>hello earth</p>"))
    d = diff_contents(prior, current)
    sd = d.sections[0]
    assert sd.current_body == "<p>hello earth</p>"
    assert len(sd.segments) > 0


# --- intra-section segments ---


def test_segments_equal_run() -> None:
    prior = _content(("a", "<p>hello world</p>"))
    current = _content(("a", "<p>hello world</p>"))
    d = diff_contents(prior, current)
    # unchanged → no segments
    assert d.sections[0].segments == ()


def test_segments_insert() -> None:
    prior = _content(("a", "<p>hello</p>"))
    current = _content(("a", "<p>hello beautiful world</p>"))
    d = diff_contents(prior, current)
    sd = d.sections[0]
    kinds = [s.kind for s in sd.segments]
    assert "insert" in kinds
    inserted = [s.text for s in sd.segments if s.kind == "insert"]
    assert any("beautiful" in t for t in inserted)


def test_segments_delete() -> None:
    prior = _content(("a", "<p>hello big world</p>"))
    current = _content(("a", "<p>hello world</p>"))
    d = diff_contents(prior, current)
    sd = d.sections[0]
    deleted = [s.text for s in sd.segments if s.kind == "delete"]
    assert any("big" in t for t in deleted)


def test_segments_replace_yields_delete_then_insert() -> None:
    prior = _content(("a", "<p>foo bar</p>"))
    current = _content(("a", "<p>foo baz</p>"))
    d = diff_contents(prior, current)
    sd = d.sections[0]
    kinds = [s.kind for s in sd.segments]
    assert "delete" in kinds
    assert "insert" in kinds
    # delete before insert for the replaced word
    del_idx = next(i for i, k in enumerate(kinds) if k == "delete")
    ins_idx = next(i for i, k in enumerate(kinds) if k == "insert")
    assert del_idx < ins_idx


def test_segments_reconstruct_words() -> None:
    prior = _content(("a", "<p>alpha beta gamma</p>"))
    current = _content(("a", "<p>alpha delta gamma</p>"))
    d = diff_contents(prior, current)
    sd = d.sections[0]
    # "beta" deleted, "delta" inserted; "alpha" and "gamma" are equal
    equal_texts = [s.text for s in sd.segments if s.kind == "equal"]
    assert any("alpha" in t for t in equal_texts)
    assert any("gamma" in t for t in equal_texts)
