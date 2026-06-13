"""Unit tests for storage/doc_render.py."""

from __future__ import annotations

from markupsafe import Markup

from feature_skills_webapp.storage.doc_content import ManifestSpec, ParsedContent, Section
from feature_skills_webapp.storage.doc_render import extract_safe_inner, render_section_doc

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
