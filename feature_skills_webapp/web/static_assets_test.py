"""Coverage guard for doc.css: asserts the curated selector vocabulary exists.

Fails loudly if a covered selector is accidentally removed.
"""

from pathlib import Path

DOC_CSS = Path(__file__).parent / "static" / "doc.css"

# Each entry is a substring that must appear in doc.css.
REQUIRED_SELECTORS = [
    # Table rules (the reported bug fix) — use rule-open patterns to avoid false matches in property values
    "table {",
    "thead",
    "tbody",
    "th {",
    "td {",
    # h4
    "h4 {",
    # blockquote
    "blockquote {",
    # hr — use rule-open pattern to avoid matching e.g. "border"
    "hr {",
    # Definition lists
    "dl {",
    "dt {",
    "dd {",
    # User-story cards
    "ol.stories",
    ".actor {",
    ".want {",
    ".scenario {",
    # Alternatives
    "ol.alternatives",
    ".alt-title {",
    ".alt-source {",
    ".alt-reason {",
    # Vision statement
    ".vision-statement {",
    # Open questions
    ".questions {",
]


def test_doc_css_covers_required_selectors() -> None:
    css = DOC_CSS.read_text()
    missing = [sel for sel in REQUIRED_SELECTORS if sel not in css]
    assert not missing, f"doc.css is missing selectors: {missing}"
