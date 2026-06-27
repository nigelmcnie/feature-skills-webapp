"""Tracker read accessors and typed mutations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from feature_skills_webapp.storage.walker import logical_key, slugify

FEATURE_STATUSES: tuple[str, ...] = ("available", "in_progress", "parked", "done", "archived")


# ---------------------------------------------------------------------------
# Read accessors
# ---------------------------------------------------------------------------


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT name FROM projects ORDER BY name").fetchall()


def get_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT id, name FROM projects WHERE name=?", (name,)).fetchone()


def get_project_row(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT id, name, repo_path FROM projects WHERE name=?", (name,)).fetchone()


def require_project(conn: sqlite3.Connection, name: str) -> int:
    """Return the project id for name, raising ProjectNotFound if absent."""
    row = conn.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()
    if row is None:
        raise ProjectNotFound(name)
    return row["id"]


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
    slug = slugify(slug)
    return conn.execute(
        "SELECT f.id, f.slug, f.status, f.owner, f.notes, p.name AS project "
        "FROM features f JOIN projects p ON f.project_id = p.id "
        "WHERE p.name = ? AND f.slug = ?",
        (project, slug),
    ).fetchone()


def require_feature(conn: sqlite3.Connection, project_id: int, slug: str) -> int:
    """Return the feature id for (project_id, slug), raising FeatureNotFound if absent."""
    from feature_skills_webapp.storage.walker import slugify as _slugify

    slug = _slugify(slug)
    row = conn.execute(
        "SELECT id FROM features WHERE project_id=? AND slug=?", (project_id, slug)
    ).fetchone()
    if row is None:
        raise FeatureNotFound(slug)
    return row["id"]


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


class ProjectExists(TrackerError): ...


class ProjectNotFound(TrackerError): ...


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


def create_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    now: str,
) -> None:
    existing = conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone()
    if existing is not None:
        raise ProjectExists(name)
    conn.execute(
        "INSERT INTO projects (name, created_at) VALUES (?, ?)",
        (name, now),
    )


def create_feature(
    conn: sqlite3.Connection,
    *,
    project: str,
    slug: str,
    notes: str | None,
    now: str,
) -> MutationResult:
    slug = slugify(slug)
    project_id = require_project(conn, project)
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
        "VALUES (NULL, 'feature_created', ?, ?)",
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
    slug = slugify(slug)
    feat = get_feature(conn, project, slug)
    if feat is None:
        raise FeatureNotFound(f"{project}/{slug}")
    if feat["status"] == "in_progress":
        return MutationResult(project, slug, "in_progress", changed=False)
    if feat["status"] not in ("available", "parked"):
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


def park_feature(
    conn: sqlite3.Connection,
    *,
    project: str,
    slug: str,
    now: str,
) -> MutationResult:
    slug = slugify(slug)
    feat = get_feature(conn, project, slug)
    if feat is None:
        raise FeatureNotFound(f"{project}/{slug}")
    if feat["status"] == "parked":
        return MutationResult(project, slug, "parked", changed=False)
    if feat["status"] == "done":
        raise InvalidTransition(f"cannot park from {feat['status']!r}")
    owner = feat["owner"]
    conn.execute(
        "UPDATE features SET status='parked', owner=NULL, updated_at=? WHERE id=?",
        (now, feat["id"]),
    )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'feature_parked', ?, ?)",
        (json.dumps({"project": project, "slug": slug, "owner": owner}), now),
    )
    return MutationResult(project, slug, "parked", changed=True)


def release_feature(
    conn: sqlite3.Connection,
    *,
    project: str,
    slug: str,
    now: str,
) -> MutationResult:
    slug = slugify(slug)
    feat = get_feature(conn, project, slug)
    if feat is None:
        raise FeatureNotFound(f"{project}/{slug}")
    if feat["status"] == "available":
        return MutationResult(project, slug, "available", changed=False)
    if feat["status"] != "in_progress":
        raise InvalidTransition(f"cannot release from {feat['status']!r}")
    owner = feat["owner"]
    conn.execute(
        "UPDATE features SET status='available', owner=NULL, updated_at=? WHERE id=?",
        (now, feat["id"]),
    )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'feature_released', ?, ?)",
        (json.dumps({"project": project, "slug": slug, "owner": owner}), now),
    )
    return MutationResult(project, slug, "available", changed=True)


def ship_feature(
    conn: sqlite3.Connection,
    *,
    project: str,
    slug: str,
    outcome: str | None,
    now: str,
) -> MutationResult:
    slug = slugify(slug)
    feat = get_feature(conn, project, slug)
    if feat is None:
        raise FeatureNotFound(f"{project}/{slug}")
    if feat["status"] == "done":
        return MutationResult(project, slug, "done", changed=False)
    if feat["status"] not in ("in_progress", "available"):
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


def drop_feature(
    conn: sqlite3.Connection,
    *,
    project: str,
    slug: str,
    now: str,
) -> MutationResult:
    slug = slugify(slug)
    feat = get_feature(conn, project, slug)
    if feat is None:
        raise FeatureNotFound(f"{project}/{slug}")
    if feat["status"] == "archived":
        return MutationResult(project, slug, "archived", changed=False)
    if feat["status"] not in ("available", "in_progress"):
        raise InvalidTransition(f"cannot drop from {feat['status']!r}")
    conn.execute(
        "UPDATE features SET status='archived', updated_at=? WHERE id=?",
        (now, feat["id"]),
    )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'feature_dropped', ?, ?)",
        (json.dumps({"project": project, "slug": slug}), now),
    )
    return MutationResult(project, slug, "archived", changed=True)


def update_feature_note(
    conn: sqlite3.Connection,
    *,
    project: str,
    slug: str,
    notes: str,
    now: str,
) -> MutationResult:
    slug = slugify(slug)
    feat = get_feature(conn, project, slug)
    if feat is None:
        raise FeatureNotFound(f"{project}/{slug}")
    if feat["notes"] == notes:
        return MutationResult(project, slug, feat["status"], changed=False)
    conn.execute(
        "UPDATE features SET notes=?, updated_at=? WHERE id=?",
        (notes, now, feat["id"]),
    )
    conn.execute(
        "INSERT INTO events (document_id, event_type, payload_json, created_at) "
        "VALUES (NULL, 'feature_note_updated', ?, ?)",
        (json.dumps({"project": project, "slug": slug}), now),
    )
    return MutationResult(project, slug, feat["status"], changed=True)


# ---------------------------------------------------------------------------
# Maintenance: one-off slug backfill
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlugConflict:
    project: str
    old_slug: str
    old_id: int
    old_status: str
    old_docs: int
    target_slug: str
    target_id: int
    target_status: str


@dataclass(frozen=True)
class SlugBackfillReport:
    renamed: list[tuple[str, str, str]]  # (project, old_slug, new_slug)
    conflicts: list[SlugConflict]


def normalise_feature_slugs(conn: sqlite3.Connection) -> SlugBackfillReport:
    """Rename feature rows whose slug is not its canonical ``slugify`` form.

    Idempotent: a second run finds nothing. Any document logical_keys embedding
    the old feature segment are rewritten to stay consistent with the new slug.

    A rename that would collide with an existing canonical-slug feature in the
    same project is **not** applied — it is reported as a conflict. Merging two
    feature rows (differing status, owner, history, and possibly both holding
    documents) is a judgement call, not a mechanical one; the guard prevents new
    collisions, so the remaining ones are surfaced for manual resolution rather
    than resolved destructively.
    """
    renamed: list[tuple[str, str, str]] = []
    conflicts: list[SlugConflict] = []
    rows = conn.execute(
        "SELECT f.id, f.project_id, f.slug, f.status, p.name AS project "
        "FROM features f JOIN projects p ON f.project_id = p.id"
    ).fetchall()
    for r in rows:
        target = slugify(r["slug"])
        if target == r["slug"]:
            continue
        other = conn.execute(
            "SELECT id, status FROM features WHERE project_id=? AND slug=? AND id<>?",
            (r["project_id"], target, r["id"]),
        ).fetchone()
        if other is not None:
            ndocs = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE feature_id=?", (r["id"],)
            ).fetchone()[0]
            conflicts.append(
                SlugConflict(
                    project=r["project"],
                    old_slug=r["slug"],
                    old_id=r["id"],
                    old_status=r["status"],
                    old_docs=ndocs,
                    target_slug=target,
                    target_id=other["id"],
                    target_status=other["status"],
                )
            )
            continue
        conn.execute("UPDATE features SET slug=? WHERE id=?", (target, r["id"]))
        for d in conn.execute(
            "SELECT id, type, instance FROM documents WHERE feature_id=?", (r["id"],)
        ).fetchall():
            conn.execute(
                "UPDATE documents SET logical_key=? WHERE id=?",
                (logical_key(r["project"], target, d["type"], d["instance"]), d["id"]),
            )
        renamed.append((r["project"], r["slug"], target))
    return SlugBackfillReport(renamed=renamed, conflicts=conflicts)
