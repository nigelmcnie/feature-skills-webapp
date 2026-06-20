"""Tests for the standalone import CLI (feature-skills-import)."""

from __future__ import annotations

from pathlib import Path

import pytest

from feature_skills_webapp.cli import main
from feature_skills_webapp.storage.db import open_db

_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="feature-doc-type" content="{doc_type}">
<title>{title}</title>
</head>
<body>
<main class="document">
<section id="content"><p>{title}</p></section>
</main>
</body>
</html>
"""


def _make_html(doc_type: str, title: str = "Test") -> str:
    return _HTML.format(doc_type=doc_type, title=title)


def _make_tree(docs_root: Path) -> None:
    (docs_root / "proj1" / "feat-a").mkdir(parents=True)
    (docs_root / "proj1" / "feat-a" / "context.html").write_text(
        _make_html("context", "feat-a context")
    )
    (docs_root / "proj1" / "feat-a" / "plan.html").write_text(_make_html("plan", "feat-a plan"))
    (docs_root / "proj1" / "feat-b").mkdir(parents=True)
    (docs_root / "proj1" / "feat-b" / "requirements.html").write_text(
        _make_html("requirements", "feat-b req")
    )


def test_cli_imports_docs_into_fresh_db(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    db_path = tmp_path / "import.db"

    rc = main(["--db", str(db_path), "--docs-root", str(docs_root)])

    assert rc == 0
    assert db_path.exists()
    with open_db(db_path) as conn:
        docs = conn.execute("SELECT type FROM documents").fetchall()
        assert len(docs) == 3
        assert {r["type"] for r in docs} == {"context", "plan", "requirements"}


def test_cli_output_matches_embedded_walk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI summary output reflects the same walk results as calling walk() directly."""
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    db_path = tmp_path / "import.db"

    main(["--db", str(db_path), "--docs-root", str(docs_root)])

    out = capsys.readouterr().out
    assert "created=3" in out
    assert "errors=0" in out


def test_cli_missing_db_arg_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--docs-root", str(tmp_path)])


def test_cli_missing_docs_root_arg_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "x.db")])


def test_cli_reconcile_marks_removed_docs_missing(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    db_path = tmp_path / "import.db"

    main(["--db", str(db_path), "--docs-root", str(docs_root)])
    (docs_root / "proj1" / "feat-b" / "requirements.html").unlink()

    rc = main(["--db", str(db_path), "--docs-root", str(docs_root), "--reconcile"])

    assert rc == 0
    with open_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM documents WHERE status = 'missing'").fetchone()[
            "n"
        ]
    assert n == 1


def test_cli_without_reconcile_does_not_mark_missing(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    _make_tree(docs_root)
    db_path = tmp_path / "import.db"

    main(["--db", str(db_path), "--docs-root", str(docs_root)])
    (docs_root / "proj1" / "feat-b" / "requirements.html").unlink()
    main(["--db", str(db_path), "--docs-root", str(docs_root)])

    with open_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM documents WHERE status = 'missing'").fetchone()[
            "n"
        ]
    assert n == 0
