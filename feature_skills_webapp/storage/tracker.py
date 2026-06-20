"""Tracker read accessors: projects, features, and feature documents."""

from __future__ import annotations

import sqlite3

FEATURE_STATUSES: tuple[str, ...] = ("available", "in_progress", "done")


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT name FROM projects ORDER BY name").fetchall()


def get_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT id, name FROM projects WHERE name=?", (name,)).fetchone()


def list_features(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.slug, f.status, f.owner, f.notes, "
        "  (SELECT MAX(e.created_at) FROM events e "
        "   JOIN documents d ON e.document_id = d.id "
        "   WHERE d.feature_id = f.id AND d.status = 'active') AS last_activity "
        "FROM features f WHERE f.project_id = ? ORDER BY f.status, f.slug",
        (project_id,),
    ).fetchall()


def get_feature(conn: sqlite3.Connection, project: str, slug: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT f.id, f.slug, f.status, f.owner, f.notes, p.name AS project "
        "FROM features f JOIN projects p ON f.project_id = p.id "
        "WHERE p.name = ? AND f.slug = ?",
        (project, slug),
    ).fetchone()


def list_feature_documents(conn: sqlite3.Connection, feature_id: int) -> list[sqlite3.Row]:
    # Active docs only; feature_id IS NULL (project-level tracker doc) can never appear.
    return conn.execute(
        "SELECT d.id, d.type, d.instance, d.logical_key, "
        "  (SELECT COALESCE(MAX(v.version_num), 0) FROM document_versions v "
        "   WHERE v.document_id = d.id) AS version "
        "FROM documents d WHERE d.feature_id = ? AND d.status = 'active' "
        "ORDER BY d.type, d.instance",
        (feature_id,),
    ).fetchall()
