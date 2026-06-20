"""Tracker read accessors and typed mutations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

FEATURE_STATUSES: tuple[str, ...] = ("available", "in_progress", "done")


# ---------------------------------------------------------------------------
# Read accessors
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Mutation errors + result
# ---------------------------------------------------------------------------


class TrackerError(Exception): ...


class FeatureNotFound(TrackerError): ...


class FeatureExists(TrackerError): ...


class InvalidTransition(TrackerError): ...


@dataclass(frozen=True)
class MutationResult:
    project: str
    slug: str
    status: str
    changed: bool


# ---------------------------------------------------------------------------
# Mutation functions
# ---------------------------------------------------------------------------


def capture_feature(
    conn: sqlite3.Connection,
    *,
    project: str,
    slug: str,
    notes: str | None,
    now: str,
) -> MutationResult:
    from feature_skills_webapp.storage.walker import upsert_project

    project_id = upsert_project(conn, project, now)
    existing = conn.execute(
        "SELECT 1 FROM features WHERE project_id=? AND slug=?", (project_id, slug)
    ).fetchone()
    if existing is not None:
        raise FeatureExists(f"{project}/{slug}")
    conn.execute(
        "INSERT INTO features (project_id, slug, status, notes, created_at, updated_at) "
        "VALUES (?, ?, 'available', ?, ?, ?)",
        (project_id, slug, notes, now, now),
    )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'feature_captured', ?, ?)",
        (json.dumps({"project": project, "slug": slug}), now),
    )
    return MutationResult(project, slug, "available", changed=True)


def claim_feature(
    conn: sqlite3.Connection,
    *,
    project: str,
    slug: str,
    owner: str,
    now: str,
) -> MutationResult:
    feat = get_feature(conn, project, slug)
    if feat is None:
        raise FeatureNotFound(f"{project}/{slug}")
    if feat["status"] == "in_progress":
        return MutationResult(project, slug, "in_progress", changed=False)
    if feat["status"] != "available":
        raise InvalidTransition(f"cannot claim from {feat['status']!r}")
    conn.execute(
        "UPDATE features SET status='in_progress', owner=?, updated_at=? WHERE id=?",
        (owner, now, feat["id"]),
    )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'feature_claimed', ?, ?)",
        (json.dumps({"project": project, "slug": slug, "owner": owner}), now),
    )
    return MutationResult(project, slug, "in_progress", changed=True)


def ship_feature(
    conn: sqlite3.Connection,
    *,
    project: str,
    slug: str,
    outcome: str | None,
    now: str,
) -> MutationResult:
    feat = get_feature(conn, project, slug)
    if feat is None:
        raise FeatureNotFound(f"{project}/{slug}")
    if feat["status"] == "done":
        return MutationResult(project, slug, "done", changed=False)
    if feat["status"] != "in_progress":
        raise InvalidTransition(f"cannot ship from {feat['status']!r}")
    if outcome is not None:
        conn.execute(
            "UPDATE features SET status='done', notes=?, updated_at=? WHERE id=?",
            (outcome, now, feat["id"]),
        )
    else:
        conn.execute(
            "UPDATE features SET status='done', updated_at=? WHERE id=?",
            (now, feat["id"]),
        )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'shipped', ?, ?)",
        (json.dumps({"project": project, "slug": slug}), now),
    )
    return MutationResult(project, slug, "done", changed=True)
