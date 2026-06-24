"""Unit tests for storage/doc_render.py."""

from __future__ import annotations

from markupsafe import Markup

from feature_skills_webapp.storage.doc_content import ManifestSpec, ParsedContent, Section
from feature_skills_webapp.storage.doc_diff import DiffSegment, DocDiff, SectionDiff
from feature_skills_webapp.storage.doc_render import (
    extract_safe_inner,
    extract_safe_inner_with_css,
    parse_feedback_items,
    render_diff,
    render_section_doc,
)

# ---------------------------------------------------------------------------
# render_section_doc
# ---------------------------------------------------------------------------

_MANIFEST = ManifestSpec(
    shape="sections",
    section_labels=(
        ("alpha", "Alpha"),
        ("beta", "Beta"),
        ("gamma", "Gamma"),
    ),
)


def _content(*keys: str) -> ParsedContent:
    return ParsedContent(
        shape="sections",
        sections=tuple(Section(key=k, body=f"<p>Body of {k}</p>") for k in keys),
    )


def test_render_section_doc_returns_markup() -> None:
    result = render_section_doc(_content("alpha"), _MANIFEST)
    assert isinstance(result, Markup)


def test_render_section_doc_wraps_sections() -> None:
    result = render_section_doc(_content("alpha", "beta"), _MANIFEST)
    assert '<section id="alpha">' in result
    assert "<p>Body of alpha</p>" in result
    assert '<section id="beta">' in result


def test_render_section_doc_manifest_ordering() -> None:
    # Stored in reverse order; manifest order should win.
    content = ParsedContent(
        shape="sections",
        sections=(
            Section(key="gamma", body="<p>Gamma</p>"),
            Section(key="alpha", body="<p>Alpha</p>"),
            Section(key="beta", body="<p>Beta</p>"),
        ),
    )
    result = render_section_doc(content, _MANIFEST)
    alpha_pos = result.index("alpha")
    beta_pos = result.index("beta")
    gamma_pos = result.index("gamma")
    assert alpha_pos < beta_pos < gamma_pos


def test_render_section_doc_unknown_keys_appended_last() -> None:
    content = ParsedContent(
        shape="sections",
        sections=(
            Section(key="unknown-key", body="<p>Unknown</p>"),
            Section(key="alpha", body="<p>Alpha</p>"),
        ),
    )
    result = render_section_doc(content, _MANIFEST)
    # alpha (in manifest) must appear before unknown-key
    assert result.index("alpha") < result.index("unknown-key")


def test_render_section_doc_keys_not_in_manifest_all_rendered() -> None:
    content = ParsedContent(
        shape="sections",
        sections=(
            Section(key="extra-1", body="<p>Extra 1</p>"),
            Section(key="extra-2", body="<p>Extra 2</p>"),
        ),
    )
    result = render_section_doc(content, _MANIFEST)
    assert "Extra 1" in result
    assert "Extra 2" in result


def test_render_section_doc_empty_manifest_renders_stored_order() -> None:
    empty_manifest = ManifestSpec(shape="sections")
    content = ParsedContent(
        shape="sections",
        sections=(
            Section(key="z", body="<p>Z</p>"),
            Section(key="a", body="<p>A</p>"),
        ),
    )
    result = render_section_doc(content, empty_manifest)
    assert result.index('"z"') < result.index('"a"')


# ---------------------------------------------------------------------------
# extract_safe_inner
# ---------------------------------------------------------------------------


def test_extract_safe_inner_returns_markup() -> None:
    result = extract_safe_inner("<html><body><p>hi</p></body></html>")
    assert isinstance(result, Markup)


def test_extract_safe_inner_returns_main_document_content() -> None:
    html = '<html><body><main class="document"><p>Inner</p></main></body></html>'
    result = extract_safe_inner(html)
    assert "<p>Inner</p>" in result
    assert "<main" not in result


def test_extract_safe_inner_falls_back_to_body() -> None:
    html = "<html><body><p>No main here</p></body></html>"
    result = extract_safe_inner(html)
    assert "<p>No main here</p>" in result


def test_extract_safe_inner_strips_script() -> None:
    html = '<html><body><main class="document"><p>Keep</p><script>alert(1)</script></main></body></html>'
    result = extract_safe_inner(html)
    assert "Keep" in result
    assert "<script>" not in result
    assert "alert" not in result


def test_extract_safe_inner_strips_style() -> None:
    html = '<html><body><main class="document"><style>body{color:red}</style><p>Keep</p></main></body></html>'
    result = extract_safe_inner(html)
    assert "Keep" in result
    assert "<style>" not in result
    assert "color:red" not in result


def test_extract_safe_inner_strips_head() -> None:
    html = "<html><head><title>Title</title></head><body><p>Body content</p></body></html>"
    result = extract_safe_inner(html)
    assert "Body content" in result
    assert "<title>" not in result
    assert "Title" not in result


def test_extract_safe_inner_excludes_outer_chrome() -> None:
    html = (
        "<html><body>"
        '<header class="doc-bar">Nav</header>'
        '<main class="document"><p>Content</p></main>'
        "</body></html>"
    )
    result = extract_safe_inner(html)
    assert "Content" in result
    assert "Nav" not in result


def test_extract_safe_inner_preserves_entities() -> None:
    html = '<html><body><main class="document"><p>a &amp; b &#160; c</p></main></body></html>'
    result = extract_safe_inner(html)
    assert "&amp;" in result
    assert "&#160;" in result


# ---------------------------------------------------------------------------
# parse_feedback_items
# ---------------------------------------------------------------------------

_NEEDS_INPUT_ARTICLE = """\
<article class="item" data-item="1">
  <header><span class="item-num">1.</span><h3>Title one</h3></header>
  <div class="detail"><p>Detail paragraph.</p></div>
  <div class="my-take"><span class="label">My take:</span> Take text.</div>
  <div class="your-thoughts"><label for="t-1">Your thoughts</label>
    <textarea id="t-1" data-item="1"></textarea>
  </div>
</article>"""

_FEEDBACK_ARTICLE = """\
<article class="item" data-item="4">
  <header><span class="item-num">4.</span><h3>Title four</h3></header>
  <div class="detail"><p>Detail four.</p></div>
  <div class="my-take"><span class="label">My take:</span> Take four.</div>
  <div class="your-thoughts"><textarea data-item="4"></textarea></div>
</article>"""

_ROUTINE_LI = """\
<li class="routine-item" data-item="9">
  <span class="item-num">9.</span>
  <span class="body">Routine body text with <em>emphasis</em>.</span>
  <button class="flag-btn" data-item="9">Flag</button>
  <div class="flag-input"><textarea data-item="9"></textarea></div>
</li>"""

_FULL_FEEDBACK_HTML = f"""\
<!DOCTYPE html>
<html><head><style>body {{ color: red; }}</style></head>
<body>
<section class="tier tier-needs-input">
<h2>Needs your input</h2>
{_NEEDS_INPUT_ARTICLE}
</section>
<section class="tier tier-feedback">
<h2>Feedback</h2>
{_FEEDBACK_ARTICLE}
</section>
<section class="tier tier-routine">
<h2>Routine</h2>
<ul class="routine-list">
{_ROUTINE_LI}
</ul>
</section>
<script>var x = 1;</script>
</body></html>"""


def test_parse_feedback_items_empty_html_returns_empty() -> None:
    assert parse_feedback_items("") == []


def test_parse_feedback_items_no_tier_sections_returns_empty() -> None:
    html = "<html><body><p>No tiers here.</p></body></html>"
    assert parse_feedback_items(html) == []


def test_parse_feedback_items_sorted_by_item_num() -> None:
    items = parse_feedback_items(_FULL_FEEDBACK_HTML)
    nums = [i.item_num for i in items]
    assert nums == sorted(nums)


def test_parse_feedback_items_needs_input_tier() -> None:
    items = parse_feedback_items(_FULL_FEEDBACK_HTML)
    item = next(i for i in items if i.item_num == 1)
    assert item.tier == "needs-input"
    assert item.kind == "response"
    assert "Title one" in item.title_html
    assert "<h3>" not in item.title_html
    assert "Detail paragraph" in item.detail_html
    assert "Take text" in item.my_take_html


def test_parse_feedback_items_feedback_tier() -> None:
    items = parse_feedback_items(_FULL_FEEDBACK_HTML)
    item = next(i for i in items if i.item_num == 4)
    assert item.tier == "feedback"
    assert item.kind == "response"
    assert "Title four" in item.title_html


def test_parse_feedback_items_routine_tier() -> None:
    items = parse_feedback_items(_FULL_FEEDBACK_HTML)
    item = next(i for i in items if i.item_num == 9)
    assert item.tier == "routine"
    assert item.kind == "routine"
    assert "Routine body text" in item.title_html
    assert "<em>emphasis</em>" in item.title_html
    assert item.detail_html == ""
    assert item.my_take_html == ""


def test_parse_feedback_items_title_has_no_outer_tag() -> None:
    items = parse_feedback_items(_FULL_FEEDBACK_HTML)
    article_item = next(i for i in items if i.item_num == 1)
    assert "<h3>" not in article_item.title_html
    assert "</h3>" not in article_item.title_html
    routine_item = next(i for i in items if i.item_num == 9)
    assert "<span" not in routine_item.title_html or "body" not in routine_item.title_html


def test_parse_feedback_items_detail_has_no_outer_div() -> None:
    items = parse_feedback_items(_FULL_FEEDBACK_HTML)
    item = next(i for i in items if i.item_num == 1)
    assert '<div class="detail">' not in item.detail_html
    assert "Detail paragraph" in item.detail_html


def test_parse_feedback_items_strips_script_and_style() -> None:
    items = parse_feedback_items(_FULL_FEEDBACK_HTML)
    for item in items:
        assert "<script" not in item.title_html
        assert "<style" not in item.title_html


def test_parse_feedback_items_nested_html_in_title() -> None:
    html = """\
<html><body>
<section class="tier tier-needs-input">
<article class="item" data-item="2">
  <header><h3>Title with <code>code</code> and <em>em</em></h3></header>
  <div class="detail"><p>Some detail.</p></div>
  <div class="my-take">My take text.</div>
  <div class="your-thoughts"><textarea data-item="2"></textarea></div>
</article>
</section>
</body></html>"""
    items = parse_feedback_items(html)
    assert len(items) == 1
    assert "<code>code</code>" in items[0].title_html
    assert "<em>em</em>" in items[0].title_html


def test_parse_feedback_items_all_three_tiers_count() -> None:
    items = parse_feedback_items(_FULL_FEEDBACK_HTML)
    assert len(items) == 3
    tiers = {i.tier for i in items}
    assert tiers == {"needs-input", "feedback", "routine"}


def test_parse_feedback_items_malformed_returns_empty_or_partial() -> None:
    result = parse_feedback_items("not html at all <><>")
    assert isinstance(result, list)


def test_parse_feedback_items_void_elements_in_body() -> None:
    html = """\
<html><body>
<section class="tier tier-needs-input">
<article class="item" data-item="3">
  <header><h3>With br tag</h3></header>
  <div class="detail"><p>Line one.<br>Line two.</p></div>
  <div class="my-take">Take.<br>More take.</div>
  <div class="your-thoughts"><textarea data-item="3"></textarea></div>
</article>
</section>
</body></html>"""
    items = parse_feedback_items(html)
    assert len(items) == 1
    assert items[0].item_num == 3
    assert "Line one." in items[0].detail_html
    assert "Line two." in items[0].detail_html


def test_parse_feedback_items_multiple_items_per_tier() -> None:
    # Boundary detection must not stop at the first article in a tier.
    html = """\
<html><body>
<section class="tier tier-needs-input">
<article class="item" data-item="1"><header><h3>One</h3></header>
  <div class="detail"><p>d1</p></div><div class="my-take">t1</div></article>
<article class="item" data-item="2"><header><h3>Two</h3></header>
  <div class="detail"><p>d2</p></div><div class="my-take">t2</div></article>
<article class="item" data-item="3"><header><h3>Three</h3></header>
  <div class="detail"><p>d3</p></div><div class="my-take">t3</div></article>
</section>
<section class="tier tier-feedback">
<article class="item" data-item="4"><header><h3>Four</h3></header>
  <div class="detail"><p>d4</p></div><div class="my-take">t4</div></article>
<article class="item" data-item="5"><header><h3>Five</h3></header>
  <div class="detail"><p>d5</p></div><div class="my-take">t5</div></article>
</section>
</body></html>"""
    items = parse_feedback_items(html)
    assert [i.item_num for i in items] == [1, 2, 3, 4, 5]
    assert next(i for i in items if i.item_num == 3).title_html == "Three"


def test_parse_feedback_items_survives_imbalanced_body_markup() -> None:
    # An item body with a net tag imbalance (here a stray </p> with no <p> — the
    # kind of markup a browser auto-corrects but html.parser reports verbatim)
    # must NOT desync boundary detection and drop the following siblings. This is
    # the bug that silently hid 6 of 8 synthesis items: a single -1 body delta
    # made the tier appear to close at the first article's </article>.
    html = """\
<html><body>
<section class="tier tier-needs-input">
<article class="item" data-item="1">
  <header><h3>One</h3></header>
  <div class="detail">text without an opening p</p></div>
  <div class="my-take">take one</div>
</article>
<article class="item" data-item="2">
  <header><h3>Two</h3></header>
  <div class="detail"><p>fine</p></div>
  <div class="my-take">take two</div>
</article>
</section>
</body></html>"""
    items = parse_feedback_items(html)
    assert [i.item_num for i in items] == [1, 2]
    assert next(i for i in items if i.item_num == 2).title_html == "Two"


# ---------------------------------------------------------------------------
# render_diff
# ---------------------------------------------------------------------------

_DIFF_MANIFEST = ManifestSpec(
    shape="sections",
    section_labels=(
        ("alpha", "Alpha"),
        ("beta", "Beta"),
    ),
)


def _make_diff(sections: list[SectionDiff]) -> DocDiff:
    return DocDiff(sections=tuple(sections))


def test_render_diff_returns_markup() -> None:
    doc_diff = _make_diff(
        [SectionDiff(key="alpha", status="unchanged", current_body="<h2>Alpha</h2><p>text</p>")]
    )
    result = render_diff(doc_diff, _DIFF_MANIFEST)
    assert isinstance(result, Markup)


def test_render_diff_unchanged_section_uses_current_body() -> None:
    doc_diff = _make_diff(
        [SectionDiff(key="alpha", status="unchanged", current_body="<h2>Alpha</h2><p>body</p>")]
    )
    result = render_diff(doc_diff, _DIFF_MANIFEST)
    assert '<section id="alpha" class="diff-unchanged">' in result
    assert "<p>body</p>" in result


def test_render_diff_added_section_uses_current_body() -> None:
    doc_diff = _make_diff(
        [SectionDiff(key="beta", status="added", current_body="<h2>Beta</h2><p>new</p>")]
    )
    result = render_diff(doc_diff, _DIFF_MANIFEST)
    assert '<section id="beta" class="diff-added">' in result
    assert "<p>new</p>" in result


def test_render_diff_removed_section_uses_prior_body() -> None:
    doc_diff = _make_diff(
        [SectionDiff(key="alpha", status="removed", prior_body="<h2>Alpha</h2><p>old</p>")]
    )
    result = render_diff(doc_diff, _DIFF_MANIFEST)
    assert '<section id="alpha" class="diff-removed">' in result
    assert "<p>old</p>" in result


def test_render_diff_changed_section_shows_ins_del() -> None:
    segments = (
        DiffSegment(kind="equal", text="hello"),
        DiffSegment(kind="delete", text="old"),
        DiffSegment(kind="insert", text="new"),
    )
    doc_diff = _make_diff(
        [SectionDiff(key="alpha", status="changed", segments=segments, current_body="")]
    )
    result = render_diff(doc_diff, _DIFF_MANIFEST)
    assert '<section id="alpha" class="diff-changed">' in result
    assert "<del>old</del>" in result
    assert "<ins>new</ins>" in result
    assert "hello" in result


def test_render_diff_changed_section_uses_manifest_label_as_heading() -> None:
    doc_diff = _make_diff(
        [SectionDiff(key="alpha", status="changed", segments=(), current_body="")]
    )
    result = render_diff(doc_diff, _DIFF_MANIFEST)
    assert "<h2>Alpha</h2>" in result


def test_render_diff_escapes_segment_text() -> None:
    segments = (DiffSegment(kind="insert", text="<script>alert(1)</script>"),)
    doc_diff = _make_diff(
        [SectionDiff(key="alpha", status="changed", segments=segments, current_body="")]
    )
    result = render_diff(doc_diff, _DIFF_MANIFEST)
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_render_diff_manifest_ordering() -> None:
    doc_diff = _make_diff(
        [
            SectionDiff(key="beta", status="unchanged", current_body="<p>B</p>"),
            SectionDiff(key="alpha", status="unchanged", current_body="<p>A</p>"),
        ]
    )
    result = render_diff(doc_diff, _DIFF_MANIFEST)
    alpha_pos = result.index('id="alpha"')
    beta_pos = result.index('id="beta"')
    assert alpha_pos < beta_pos


# ---------------------------------------------------------------------------
# extract_safe_inner_with_css — scope-and-keep + containment
# ---------------------------------------------------------------------------

_OPAQUE_WITH_STYLE = """\
<!DOCTYPE html><html><body>
<main class="document">
<style>table { border: 1px solid red }</style>
<p>Hello</p>
</main>
</body></html>
"""

_OPAQUE_WITH_IMPORT = """\
<!DOCTYPE html><html><body>
<main class="document">
<style>@import url("foo.css"); @charset "UTF-8"; @namespace svg "http://x";
table { color: blue }</style>
<p>Content</p>
</main>
</body></html>
"""

_OPAQUE_WITH_SCRIPT = """\
<!DOCTYPE html><html><body>
<main class="document">
<style>p { color: green }</style>
<script>alert(1)</script>
<p>Safe</p>
</main>
</body></html>
"""


def test_extract_safe_inner_with_css_captures_style_text() -> None:
    inner, css = extract_safe_inner_with_css(_OPAQUE_WITH_STYLE)
    assert "table" in css
    assert "border" in css


def test_extract_safe_inner_with_css_style_not_in_body() -> None:
    inner, _ = extract_safe_inner_with_css(_OPAQUE_WITH_STYLE)
    assert "<style>" not in str(inner)
    assert "Hello" in str(inner)


def test_extract_safe_inner_with_css_drops_import_charset_namespace() -> None:
    _, css = extract_safe_inner_with_css(_OPAQUE_WITH_IMPORT)
    assert "@import" not in css
    assert "@charset" not in css
    assert "@namespace" not in css
    assert "color: blue" in css


def test_extract_safe_inner_with_css_still_strips_script() -> None:
    inner, _ = extract_safe_inner_with_css(_OPAQUE_WITH_SCRIPT)
    assert "<script>" not in str(inner)
    assert "alert" not in str(inner)
    assert "Safe" in str(inner)


def test_extract_safe_inner_with_css_neutralises_style_close_tag() -> None:
    # A literal </style> in the CSS must not terminate the <style> element early.
    html = (
        "<!DOCTYPE html><html><body><main class='document'>"
        "<style>p { color: red } </style> .evil { display:none }</style>"
        "<p>ok</p></main></body></html>"
    )
    _, css = extract_safe_inner_with_css(html)
    # The literal </style> must have been neutralised, not left as-is.
    assert "</style>" not in css


def test_extract_safe_inner_with_css_no_style_returns_empty_string() -> None:
    html = "<!DOCTYPE html><html><body><main class='document'><p>x</p></main></body></html>"
    inner, css = extract_safe_inner_with_css(html)
    assert css == ""
    assert "x" in str(inner)
