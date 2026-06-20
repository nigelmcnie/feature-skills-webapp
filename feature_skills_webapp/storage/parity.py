"""Normalised section-text parity check between two DBs.

Compares documents that share a logical_key in both connections.
Ignores by-construction differences: source_path, actor, metadata_json,
and event provenance — these live outside content_json and are naturally
excluded by loading via current_content().
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from feature_skills_webapp.storage.doc_diff import extract_text
from feature_skills_webapp.storage.versions import current_content


@dataclass(frozen=True)
class SectionMismatch:
    logical_key: str
    section_key: str
    text_a: str
    text_b: str


@dataclass(frozen=True)
class ParityReport:
    mismatches: tuple[SectionMismatch, ...]
    only_in_a: tuple[str, ...]  # logical_keys present in conn_a but not conn_b
    only_in_b: tuple[str, ...]  # logical_keys present in conn_b but not conn_a

    @property
    def ok(self) -> bool:
        return not (self.mismatches or self.only_in_a or self.only_in_b)


def _docs_by_key(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT logical_key, id FROM documents WHERE logical_key IS NOT NULL"
    ).fetchall()
    return {r["logical_key"]: r["id"] for r in rows}


def compare_dbs(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection) -> ParityReport:
    """Compare normalised section text per logical_key between two DBs.

    For each key present in both DBs, loads the latest version's sections and
    compares extract_text(body) per section key — whitespace and tag differences
    are normalised away.  Keys absent from one side are reported separately.
    """
    keys_a = _docs_by_key(conn_a)
    keys_b = _docs_by_key(conn_b)

    shared = set(keys_a) & set(keys_b)
    only_in_a = tuple(sorted(set(keys_a) - set(keys_b)))
    only_in_b = tuple(sorted(set(keys_b) - set(keys_a)))

    mismatches: list[SectionMismatch] = []
    for key in sorted(shared):
        content_a = current_content(conn_a, keys_a[key])
        content_b = current_content(conn_b, keys_b[key])
        if content_a is None or content_b is None:
            continue
        texts_a = {s.key: extract_text(s.body) for s in content_a.sections}
        texts_b = {s.key: extract_text(s.body) for s in content_b.sections}
        for sk in sorted(set(texts_a) | set(texts_b)):
            ta = texts_a.get(sk, "")
            tb = texts_b.get(sk, "")
            if ta != tb:
                mismatches.append(
                    SectionMismatch(logical_key=key, section_key=sk, text_a=ta, text_b=tb)
                )

    return ParityReport(
        mismatches=tuple(mismatches),
        only_in_a=only_in_a,
        only_in_b=only_in_b,
    )
