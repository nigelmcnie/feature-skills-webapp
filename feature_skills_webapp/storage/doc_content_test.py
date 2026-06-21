"""Unit tests for storage/doc_content.py."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from feature_skills_webapp.storage.doc_content import (
    ManifestSpec,
    ParsedContent,
    Section,
    humanise_section_key,
    manifest_for,
    parse_content,
    serialise,
)

# ---------------------------------------------------------------------------
# Fixtures — HTML fragments used across multiple tests
# ---------------------------------------------------------------------------

_CONTEXT_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="context">
<title>Test — Context</title>
</head>
<body>
<main class="document">
<header class="doc-header"><h1>Test</h1><p class="subtitle">Subtitle</p></header>
<section id="problem-space"><p>Problem text &amp; more</p></section>
<section id="related-work"><p>Related <a href="#">work</a></p></section>
<section id="constraints"><p>Constraints here</p></section>
<section id="open-questions"><p>Open questions</p></section>
</main>
<div class="comment-trigger" id="comment-trigger">&#x1F4AC; Comment</div>
<div class="comment-popover" id="comment-popover">
  <div class="context-excerpt" id="popover-excerpt"></div>
  <textarea id="popover-textarea" placeholder="Note…"></textarea>
  <div class="actions">
    <button id="popover-cancel">Cancel</button>
    <button id="popover-save">Save</button>
  </div>
</div>
<footer class="actions">
  <div class="status"><span class="count" id="footer-count">0</span> comments</div>
</footer>
</body>
</html>
"""

_REQUIREMENTS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="requirements">
</head>
<body>
<main class="document">
<header class="doc-header"><h1>Requirements</h1></header>
<section id="problem"><p>The problem.</p></section>
<section id="vision"><p>The vision.</p></section>
<section id="user-stories"><ul><li>As a user…</li></ul></section>
<section id="data-model"><p>Schema here.</p></section>
<section id="technical-approach"><p>Approach.</p></section>
<section id="alternatives"><p>Alternatives.</p></section>
<section id="delivery-phases"><p>Phases.</p></section>
<section id="indicative-notes"><p>Notes.</p></section>
</main>
</body>
</html>
"""

_PLAN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="plan">
</head>
<body>
<main class="document">
<header class="doc-header"><h1>Plan</h1></header>
<section id="overview"><p>Overview text.</p></section>
<section id="key-decisions"><ol><li>Decision one.</li></ol></section>
<section id="file-structure"><p>Files go here.</p></section>
<section id="phase-1"><p>Phase 1 content.</p></section>
<section id="phase-2"><p>Phase 2 content.</p></section>
<section id="qc"><pre><code>uv run pytest</code></pre></section>
<section id="checklist"><ul><li>Item one.</li></ul></section>
</main>
</body>
</html>
"""

_FEATURES_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="features">
<title>Project — Features</title>
</head>
<body>
<main class="document">
<section id="in-progress"><table><tbody>
  <tr><td class="feature-name">my-feature</td></tr>
</tbody></table></section>
</main>
</body>
</html>
"""

_FEEDBACK_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>My Feature — Requirements Feedback #1</title>
</head>
<body>
<div class="review-block">
  <h2>Summary</h2>
  <p>Feedback content here.</p>
</div>
</body>
</html>
"""

_NO_MAIN_HTML = """\
<!DOCTYPE html>
<html><body><p>No main element here.</p></body></html>
"""

_NESTED_SECTION_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body>
<main class="document">
<header class="doc-header"><h1>Nested</h1></header>
<section id="phase-1">
<p>Outer content</p>
<section id="nested-inner">
<p>Inner nested content — should stay in phase-1 body</p>
</section>
<p>After nested</p>
</section>
<section id="phase-2"><p>Second top-level section</p></section>
</main>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# manifest_for
# ---------------------------------------------------------------------------


def test_manifest_for_context() -> None:
    spec = manifest_for("context")
    assert spec.shape == "sections"
    assert "problem-space" in spec.expected_keys
    assert "open-questions" in spec.expected_keys


def test_manifest_for_context_section_labels_ordered() -> None:
    spec = manifest_for("context")
    keys = [k for k, _ in spec.section_labels]
    assert keys[0] == "problem-space"
    assert "open-questions" in keys
    # Each entry is a (key, label) pair with a non-empty label
    for key, label in spec.section_labels:
        assert key
        assert label


def test_manifest_for_requirements() -> None:
    spec = manifest_for("requirements")
    assert spec.shape == "sections"
    assert "problem" in spec.expected_keys
    assert "delivery-phases" in spec.expected_keys


def test_manifest_for_requirements_section_labels_ordered() -> None:
    spec = manifest_for("requirements")
    keys = [k for k, _ in spec.section_labels]
    assert keys[0] == "summary"
    assert keys[1] == "problem"
    assert "delivery-phases" in keys
    for key, label in spec.section_labels:
        assert key
        assert label


def test_manifest_for_plan() -> None:
    spec = manifest_for("plan")
    assert spec.shape == "sections"
    assert "phase-" in spec.repeated_prefixes
    assert "overview" in spec.expected_keys
    assert "checklist" in spec.expected_keys


def test_manifest_for_plan_section_labels_ordered() -> None:
    spec = manifest_for("plan")
    keys = [k for k, _ in spec.section_labels]
    assert keys[0] == "overview"
    assert "checklist" in keys
    for key, label in spec.section_labels:
        assert key
        assert label


def test_manifest_expected_keys_derives_from_section_labels() -> None:
    from feature_skills_webapp.storage.doc_content import ManifestSpec

    spec = ManifestSpec(
        shape="sections",
        section_labels=(("alpha", "Alpha"), ("beta", "Beta"), ("gamma", "Gamma")),
    )
    assert spec.expected_keys == ("alpha", "beta", "gamma")


def test_manifest_for_features_is_opaque() -> None:
    assert manifest_for("features").shape == "opaque"


def test_manifest_for_feedback_is_opaque() -> None:
    assert manifest_for("requirements-feedback").shape == "opaque"
    assert manifest_for("context-feedback").shape == "opaque"
    assert manifest_for("plan-feedback").shape == "opaque"


def test_manifest_for_unknown_is_opaque() -> None:
    assert manifest_for("totally-unknown-type").shape == "opaque"


# ---------------------------------------------------------------------------
# parse_content — section-parsed docs
# ---------------------------------------------------------------------------


def test_parse_context_sections() -> None:
    spec = manifest_for("context")
    result = parse_content(_CONTEXT_HTML, spec)
    assert result.shape == "sections"
    keys = [s.key for s in result.sections]
    assert keys == ["problem-space", "related-work", "constraints", "open-questions"]


def test_parse_requirements_sections() -> None:
    spec = manifest_for("requirements")
    result = parse_content(_REQUIREMENTS_HTML, spec)
    assert result.shape == "sections"
    keys = [s.key for s in result.sections]
    assert "problem" in keys
    assert "data-model" in keys
    assert len(keys) > 0


def test_parse_plan_sections() -> None:
    spec = manifest_for("plan")
    result = parse_content(_PLAN_HTML, spec)
    assert result.shape == "sections"
    keys = [s.key for s in result.sections]
    assert "overview" in keys
    assert "phase-1" in keys
    assert "phase-2" in keys
    assert "checklist" in keys


def test_plan_repeated_sections_allowed() -> None:
    """Plan can have phase-N sections (any N)."""
    spec = manifest_for("plan")
    result = parse_content(_PLAN_HTML, spec)
    phase_keys = [k for k in [s.key for s in result.sections] if k.startswith("phase-")]
    assert len(phase_keys) >= 1


def test_section_body_is_inner_html_not_outer() -> None:
    """Section body is the inner HTML — the <section id="..."> tag itself is excluded."""
    spec = manifest_for("context")
    result = parse_content(_CONTEXT_HTML, spec)
    problem = next(s for s in result.sections if s.key == "problem-space")
    assert "<section" not in problem.body
    assert "Problem text" in problem.body


def test_header_excluded_from_sections() -> None:
    """<header class="doc-header"> is not captured — it's not a <section>."""
    spec = manifest_for("context")
    result = parse_content(_CONTEXT_HTML, spec)
    all_bodies = " ".join(s.body for s in result.sections)
    assert "doc-header" not in all_bodies
    assert "Subtitle" not in all_bodies


def test_chrome_excluded_from_sections() -> None:
    """comment-trigger, comment-popover, footer are outside <main> — never in section bodies."""
    spec = manifest_for("context")
    result = parse_content(_CONTEXT_HTML, spec)
    all_bodies = " ".join(s.body for s in result.sections)
    assert "comment-trigger" not in all_bodies
    assert "comment-popover" not in all_bodies
    assert "popover-cancel" not in all_bodies
    assert 'class="actions"' not in all_bodies
    assert "footer-count" not in all_bodies


def test_nested_section_stays_in_body() -> None:
    """A <section> nested inside a top-level section is captured as part of the body."""
    spec = ManifestSpec(shape="sections")
    result = parse_content(_NESTED_SECTION_HTML, spec)
    keys = [s.key for s in result.sections]
    # Only two top-level sections; nested-inner is NOT extracted as a separate section.
    assert "phase-1" in keys
    assert "phase-2" in keys
    assert "nested-inner" not in keys
    phase1 = next(s for s in result.sections if s.key == "phase-1")
    assert "nested-inner" in phase1.body
    assert "Inner nested content" in phase1.body
    assert "After nested" in phase1.body


# ---------------------------------------------------------------------------
# parse_content — opaque docs
# ---------------------------------------------------------------------------


def test_parse_opaque_features() -> None:
    spec = manifest_for("features")
    result = parse_content(_FEATURES_HTML, spec)
    assert result.shape == "opaque"
    assert len(result.sections) == 1
    assert result.sections[0].key == ""
    assert result.sections[0].body == _FEATURES_HTML


def test_parse_opaque_feedback() -> None:
    spec = manifest_for("requirements-feedback")
    result = parse_content(_FEEDBACK_HTML, spec)
    assert result.shape == "opaque"
    assert len(result.sections) == 1
    assert result.sections[0].key == ""
    assert result.sections[0].body == _FEEDBACK_HTML


# ---------------------------------------------------------------------------
# Graceful classification — no <main> or zero sections
# ---------------------------------------------------------------------------


def test_no_main_returns_sentinel() -> None:
    spec = manifest_for("context")
    result = parse_content(_NO_MAIN_HTML, spec)
    assert result.shape == "sections"
    assert result.sections == ()


def test_empty_main_returns_sentinel() -> None:
    empty_main = "<html><body><main class='document'></main></body></html>"
    spec = manifest_for("context")
    result = parse_content(empty_main, spec)
    assert result.shape == "sections"
    assert result.sections == ()


def test_main_without_document_class_is_ignored() -> None:
    html = """\
<html><body>
<main class="other-class">
<section id="foo"><p>Text</p></section>
</main>
</body></html>
"""
    spec = manifest_for("context")
    result = parse_content(html, spec)
    assert result.sections == ()


# ---------------------------------------------------------------------------
# Entity faithfulness
# ---------------------------------------------------------------------------


def _make_section_html(body_content: str) -> str:
    return (
        "<html><body>"
        '<main class="document">'
        '<section id="s1">' + body_content + "</section>"
        "</main>"
        "</body></html>"
    )


def test_entity_roundtrip_amp() -> None:
    """&amp; is preserved as &amp; (not decoded to &)."""
    html = _make_section_html("<p>a &amp; b</p>")
    spec = ManifestSpec(shape="sections")
    result = parse_content(html, spec)
    body = result.sections[0].body
    assert "&amp;" in body
    assert "a & b" not in body  # not decoded


def test_entity_double_amp_distinct_from_single_amp() -> None:
    """&amp;amp; and bare &amp; produce different serialisations."""
    html_double = _make_section_html("<p>&amp;amp;</p>")
    html_single = _make_section_html("<p>&amp;</p>")
    spec = ManifestSpec(shape="sections")
    s_double = serialise(parse_content(html_double, spec))
    s_single = serialise(parse_content(html_single, spec))
    assert s_double != s_single


def test_char_ref_roundtrip() -> None:
    """Numeric char refs like &#160; are preserved, not decoded."""
    html = _make_section_html("<p>non&#160;breaking</p>")
    spec = ManifestSpec(shape="sections")
    result = parse_content(html, spec)
    body = result.sections[0].body
    assert "&#160;" in body


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_serialise_deterministic() -> None:
    """Same input bytes always produce the same serialise() output."""
    spec = manifest_for("context")
    s1 = serialise(parse_content(_CONTEXT_HTML, spec))
    s2 = serialise(parse_content(_CONTEXT_HTML, spec))
    assert s1 == s2


def test_content_edit_changes_serialisation() -> None:
    """Changing a section's content produces a different serialisation."""
    spec = manifest_for("context")
    s_orig = serialise(parse_content(_CONTEXT_HTML, spec))
    modified = _CONTEXT_HTML.replace("Problem text &amp; more", "Completely different text")
    s_modified = serialise(parse_content(modified, spec))
    assert s_orig != s_modified


def test_new_section_changes_serialisation() -> None:
    """Adding a section changes the serialisation."""
    spec = manifest_for("requirements")
    s_orig = serialise(parse_content(_REQUIREMENTS_HTML, spec))
    with_extra = _REQUIREMENTS_HTML.replace(
        "</main>",
        '<section id="design-notes"><p>Design notes added.</p></section>\n</main>',
    )
    s_modified = serialise(parse_content(with_extra, spec))
    assert s_orig != s_modified


# ---------------------------------------------------------------------------
# serialise() format
# ---------------------------------------------------------------------------


def test_serialise_format() -> None:
    """serialise() produces compact JSON with expected keys."""
    content = ParsedContent(
        shape="sections",
        sections=(
            Section(key="foo", body="<p>Bar</p>"),
            Section(key="baz", body="<p>Qux &amp; more</p>"),
        ),
    )
    out = serialise(content)
    parsed = json.loads(out)
    assert parsed["shape"] == "sections"
    assert len(parsed["sections"]) == 2
    assert parsed["sections"][0] == {"key": "foo", "body": "<p>Bar</p>"}
    # Compact — no spaces after separators
    assert ", " not in out
    assert ": " not in out


def test_serialise_opaque_format() -> None:
    content = ParsedContent(
        shape="opaque",
        sections=(Section(key="", body="<html>whole doc</html>"),),
    )
    out = serialise(content)
    parsed = json.loads(out)
    assert parsed["shape"] == "opaque"
    assert parsed["sections"][0]["key"] == ""


def test_serialise_non_ascii_preserved() -> None:
    """Non-ASCII characters are not escaped (ensure_ascii=False)."""
    content = ParsedContent(
        shape="opaque",
        sections=(Section(key="", body="<p>café résumé 日本語</p>"),),
    )
    out = serialise(content)
    assert "café" in out
    assert "日本語" in out


# ---------------------------------------------------------------------------
# Corpus test — skipped if dev-store is absent (CI without store still passes)
# ---------------------------------------------------------------------------

_STORE = pathlib.Path.home() / ".claude" / "feature-docs"
# Strict invariants are scoped to feature-skills-webapp, which uses the standard templates
# this parser was built for.  Other projects in the store (kea/, planning/) use different
# template schemas or may have unescaped HTML in code blocks — they get the graceful
# sentinel and are covered only by the determinism test.
_STRICT_PROJECT = "feature-skills-webapp"
_SECTION_DOC_TYPES = {"context", "requirements", "plan"}
_OPAQUE_DOC_TYPES = {"features"}

# Doc type inferred from filename stem for feedback docs — mirrors walker.feedback_type().
_FEEDBACK_STEM_RE = re.compile(r"^(?P<phase>[a-z]+)-feedback-\d+$")


def _doc_type_for_path(path: pathlib.Path) -> str | None:
    """Cheap doc-type inference from path — mirrors walker logic for the corpus test."""
    stem = path.stem
    # features.html at project root
    if stem == "features":
        return "features"
    # Feedback files like requirements-feedback-1.html
    m = _FEEDBACK_STEM_RE.match(stem)
    if m:
        return f"{m.group('phase')}-feedback"
    # Regular feature docs: stem is the doc type (context, requirements, plan)
    if stem in _SECTION_DOC_TYPES:
        return stem
    return None


def _is_strict_project(path: pathlib.Path) -> bool:
    """True if this path belongs to the strictly-checked project."""
    try:
        rel = path.relative_to(_STORE)
        return rel.parts[0] == _STRICT_PROJECT
    except ValueError:
        return False


@pytest.mark.skipif(not _STORE.exists(), reason="dev-store absent")
def test_corpus_section_docs_parse_nonempty() -> None:
    """feature-skills-webapp context/requirements/plan docs yield ≥1 section."""
    failures: list[str] = []
    for html_path in sorted(_STORE.rglob("*.html")):
        if not _is_strict_project(html_path):
            continue
        doc_type = _doc_type_for_path(html_path)
        if doc_type not in _SECTION_DOC_TYPES:
            continue
        spec = manifest_for(doc_type)
        html = html_path.read_text(encoding="utf-8", errors="replace")
        result = parse_content(html, spec)
        if not result.sections:
            failures.append(str(html_path))
        else:
            for section in result.sections:
                assert section.key, f"empty key in {html_path}"
    if failures:
        pytest.fail("Section docs with no parsed sections:\n" + "\n".join(failures))


@pytest.mark.skipif(not _STORE.exists(), reason="dev-store absent")
def test_corpus_plan_has_phase_section() -> None:
    """feature-skills-webapp plan docs have at least one phase-N section."""
    failures: list[str] = []
    for html_path in sorted(_STORE.rglob("*.html")):
        if not _is_strict_project(html_path):
            continue
        if html_path.stem != "plan":
            continue
        spec = manifest_for("plan")
        html = html_path.read_text(encoding="utf-8", errors="replace")
        result = parse_content(html, spec)
        phase_keys = [s.key for s in result.sections if s.key.startswith("phase-")]
        if not phase_keys:
            failures.append(str(html_path))
    if failures:
        pytest.fail("Plan docs with no phase-N section:\n" + "\n".join(failures))


@pytest.mark.skipif(not _STORE.exists(), reason="dev-store absent")
def test_corpus_section_keys_subset_of_manifest() -> None:
    """Section keys in feature-skills-webapp docs are a subset of the manifest."""
    violations: list[str] = []
    for html_path in sorted(_STORE.rglob("*.html")):
        if not _is_strict_project(html_path):
            continue
        doc_type = _doc_type_for_path(html_path)
        if doc_type not in _SECTION_DOC_TYPES:
            continue
        spec = manifest_for(doc_type)
        html = html_path.read_text(encoding="utf-8", errors="replace")
        result = parse_content(html, spec)
        for section in result.sections:
            key = section.key
            in_expected = key in spec.expected_keys
            in_repeated = any(key.startswith(p) for p in spec.repeated_prefixes)
            if not in_expected and not in_repeated:
                violations.append(f"{html_path}: unexpected key '{key}'")
    if violations:
        pytest.fail("Unexpected section keys found:\n" + "\n".join(violations))


@pytest.mark.skipif(not _STORE.exists(), reason="dev-store absent")
def test_corpus_feedback_and_features_are_opaque() -> None:
    """feedback/*.html and features.html always parse as opaque (all projects)."""
    for html_path in sorted(_STORE.rglob("*.html")):
        doc_type = _doc_type_for_path(html_path)
        if doc_type is None:
            continue
        spec = manifest_for(doc_type)
        if spec.shape != "opaque":
            continue
        html = html_path.read_text(encoding="utf-8", errors="replace")
        result = parse_content(html, spec)
        assert result.shape == "opaque", f"{html_path} should be opaque"
        assert len(result.sections) == 1
        assert result.sections[0].key == ""


@pytest.mark.skipif(not _STORE.exists(), reason="dev-store absent")
def test_corpus_determinism() -> None:
    """For each doc in the store, parse+serialise twice → identical output."""
    for html_path in sorted(_STORE.rglob("*.html")):
        doc_type = _doc_type_for_path(html_path)
        if doc_type is None:
            continue
        spec = manifest_for(doc_type)
        html = html_path.read_text(encoding="utf-8", errors="replace")
        s1 = serialise(parse_content(html, spec))
        s2 = serialise(parse_content(html, spec))
        assert s1 == s2, f"Non-deterministic serialisation for {html_path}"


def test_humanise_section_key_known_label() -> None:
    labels = dict(manifest_for("context").section_labels)
    assert humanise_section_key("problem-space", labels) == "Problem space"


def test_humanise_section_key_unknown_multiword_is_sentence_case() -> None:
    # Unknown keys follow the manifest's sentence-case convention ("Open questions"),
    # NOT Title Case — so the inbox card label and the diff heading can't drift apart.
    assert humanise_section_key("open-questions", {}) == "Open questions"


def test_humanise_section_key_handles_underscores() -> None:
    assert humanise_section_key("key_decisions", {}) == "Key decisions"
