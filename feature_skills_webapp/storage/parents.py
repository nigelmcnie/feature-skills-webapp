"""Parent-entity helpers: slug normalisation, document identity, project/feature upsert.

Kept separate from walker.py so that documents.py can import logical_key without
creating an import cycle when walker.py calls submit_document from documents.py.
Walker.py re-exports these symbols for backward compatibility.
"""

from __future__ import annotations

import re
import sqlite3


def slugify(text: str) -> str:
    """Canonicalise a feature name into a kebab-case slug.

    Lowercase; every run of non-alphanumeric characters collapses to a single
    '-'; leading/trailing '-' stripped. Idempotent on already-kebab input.

    This is the single rule for feature identity. Without it a display name
    ("File classification") and its slug ("file-classification") diverge into
    two feature rows: a bulk tracker import once stored display names verbatim
    as slugs, and the per-document write path keys on the slugified directory
    name — so the two could never reconcile and spawned duplicate features.

    Defined here (not walker.py) to avoid an import cycle once walker imports
    submit_document from documents.py; walker.py re-exports it unchanged.
    """
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def logical_key(project: str, feature: str | None, doc_type: str, instance: int) -> str:
    """Canonical stable identity key for a document: '{project}/{feature or '-'}/{doc_type}/{instance}'.

    The feature segment is slugified so a document's identity can never diverge
    from its feature's canonical slug (see ``slugify``).
    """
    seg = slugify(feature) if feature else "-"
    return f"{project}/{seg}/{doc_type}/{instance}"


def upsert_project(conn: sqlite3.Connection, name: str, now: str) -> int:
    conn.execute(
        "INSERT INTO projects (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING",
        (name, now),
    )
    return conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()["id"]


def upsert_feature(conn: sqlite3.Connection, project_id: int, slug: str, now: str) -> int:
    slug = slugify(slug)
    conn.execute(
        "INSERT INTO features (project_id, slug, status, created_at, updated_at) "
        "VALUES (?, ?, 'available', ?, ?) "
        "ON CONFLICT(project_id, slug) DO NOTHING",
        (project_id, slug, now, now),
    )
    return conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug=?", (project_id, slug)
    ).fetchone()["id"]
