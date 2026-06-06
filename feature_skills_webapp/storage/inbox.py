"""Inbox read model: cross-project summary of new, in-progress, and recently shipped features."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SHIPPED_RECENT_DAYS = 30
SHIPPED_LIMIT = 5

DOC_TYPE_ORDER = ["context", "requirements", "plan", "review"]

_TYPE_LABELS = {
    "context": "Context",
    "requirements": "Requirements",
    "plan": "Plan",
    "review": "Review",
    "features": "Tracker",
}


def humanise_type(doc_type: str) -> str:
    if doc_type in _TYPE_LABELS:
        return _TYPE_LABELS[doc_type]
    return doc_type.replace("-", " ").replace("_", " ").capitalize()


@dataclass(frozen=True)
class InboxCard:
    project: str
    feature: str | None
    label: str
    last_activity: str | None
    document_id: int | None = None


@dataclass(frozen=True)
class Inbox:
    new_since: list[InboxCard]
    in_progress: list[InboxCard]
    recently_shipped: list[InboxCard]

    @property
    def is_empty(self) -> bool:
        """True when no category has any cards — drives the inbox's all-empty state."""
        return not (self.new_since or self.in_progress or self.recently_shipped)


def _doc_card(r: sqlite3.Row) -> InboxCard:
    return InboxCard(
        project=r["project"],
        feature=r["feature"],
        label=humanise_type(r["doc_type"]),
        last_activity=r["last_activity"],
        document_id=r["document_id"],
    )


def _feature_card(r: sqlite3.Row, *, label: str) -> InboxCard:
    return InboxCard(
        project=r["project"],
        feature=r["feature"],
        label=label,
        last_activity=r["last_activity"],
    )


def _shipped_card(r: sqlite3.Row) -> InboxCard:
    return InboxCard(
        project=r["project"],
        feature=r["slug"],
        label="Shipped",
        last_activity=r["shipped_at"],
    )


def new_since_last_visit(
    conn: sqlite3.Connection, project_id: int | None = None
) -> list[InboxCard]:
    sql = (
        "SELECT d.id AS document_id, d.type AS doc_type, p.name AS project, "
        "  f.slug AS feature, "
        "  (SELECT MAX(e.created_at) FROM events e WHERE e.document_id = d.id) AS last_activity "
        "FROM documents d "
        "JOIN projects p ON d.project_id = p.id "
        "JOIN features  f ON d.feature_id = f.id "
        "WHERE d.status = 'active' AND EXISTS ("
        "  SELECT 1 FROM events e WHERE e.document_id = d.id "
        "  AND e.created_at > COALESCE("
        "    (SELECT last_read_at FROM read_state WHERE document_id = d.id), ''))"
    )
    params: list[object] = []
    if project_id is not None:
        sql += " AND d.project_id = ?"  # noqa: S608
        params.append(project_id)
    # document_id is a stable secondary key so ties on last_activity have a deterministic order.
    sql += " ORDER BY last_activity DESC, document_id DESC"
    return [_doc_card(r) for r in conn.execute(sql, params).fetchall()]


def in_progress(conn: sqlite3.Connection, project_id: int | None = None) -> list[InboxCard]:
    sql = (
        "SELECT p.name AS project, f.slug AS feature, "
        "  (SELECT MAX(e.created_at) FROM events e "
        "   JOIN documents d ON e.document_id = d.id "
        "   WHERE d.feature_id = f.id AND d.status='active') AS last_activity "
        "FROM features f JOIN projects p ON f.project_id = p.id "
        "WHERE f.status = 'in_progress'"
    )
    params: list[object] = []
    if project_id is not None:
        sql += " AND f.project_id = ?"  # noqa: S608
        params.append(project_id)
    # f.slug is a stable secondary key so ties on last_activity have a deterministic order.
    sql += " ORDER BY COALESCE(last_activity,'') DESC, f.slug"
    return [_feature_card(r, label="In progress") for r in conn.execute(sql, params).fetchall()]


def recently_shipped(
    conn: sqlite3.Connection,
    project: str | None = None,
    *,
    limit: int = SHIPPED_LIMIT,
    within_days: int = SHIPPED_RECENT_DAYS,
) -> list[InboxCard]:
    cutoff = (datetime.now(tz=UTC) - timedelta(days=within_days)).isoformat()
    sql = (
        "SELECT json_extract(payload_json,'$.project') AS project, "
        "  json_extract(payload_json,'$.slug') AS slug, "
        "  MAX(created_at) AS shipped_at "
        "FROM events WHERE event_type = 'shipped' AND created_at > ?"
    )
    params: list[object] = [cutoff]
    if project is not None:
        sql += " AND json_extract(payload_json,'$.project') = ?"  # noqa: S608
        params.append(project)
    sql += " GROUP BY project, slug ORDER BY shipped_at DESC LIMIT ?"
    params.append(limit)
    return [_shipped_card(r) for r in conn.execute(sql, params).fetchall()]


def build_inbox(conn: sqlite3.Connection, project: str | None = None) -> Inbox:
    project_id: int | None = None
    if project is not None:
        row = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()
        if row is None:
            return Inbox([], [], [])
        project_id = row["id"]
    return Inbox(
        new_since=new_since_last_visit(conn, project_id),
        in_progress=in_progress(conn, project_id),
        recently_shipped=recently_shipped(conn, project),
    )
