"""Inbox read model: cross-project summary of new, in-progress, and recently shipped features."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from feature_skills_webapp.storage.doc_content import humanise_section_key, manifest_for
from feature_skills_webapp.storage.doc_diff import diff_contents
from feature_skills_webapp.storage.read_state import last_read_at, mark_documents_read
from feature_skills_webapp.storage.versions import content_at_or_before, current_content
from feature_skills_webapp.storage.walker import FEEDBACK_SUFFIX

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


def badge_kind(doc_type: str | None) -> str:
    if doc_type is None:
        return "context"
    if doc_type.endswith(FEEDBACK_SUFFIX):
        return "feedback"
    return doc_type


def doc_type_rank(doc_type: str) -> int:
    """Sort key placing known doc types in DOC_TYPE_ORDER, unknowns last."""
    return DOC_TYPE_ORDER.index(doc_type) if doc_type in DOC_TYPE_ORDER else len(DOC_TYPE_ORDER)


@dataclass(frozen=True)
class InboxReason:
    kind: Literal["new", "content", "comments"]
    label: str
    changed_count: int = 0
    has_diff: bool = False


@dataclass(frozen=True)
class InboxCard:
    project: str
    feature: str | None
    label: str
    last_activity: str | None
    document_id: int | None = None
    badge: str = "context"
    reason: InboxReason | None = None
    href: str | None = None


@dataclass(frozen=True)
class Inbox:
    new_since: list[InboxCard]
    in_progress: list[InboxCard]
    parked: list[InboxCard]
    recently_shipped: list[InboxCard]
    awaiting_input: list[InboxCard]

    @property
    def is_empty(self) -> bool:
        """True when no category has any cards — drives the inbox's all-empty state."""
        return not (
            self.new_since
            or self.in_progress
            or self.parked
            or self.recently_shipped
            or self.awaiting_input
        )


def _doc_card(r: sqlite3.Row) -> InboxCard:
    return InboxCard(
        project=r["project"],
        feature=r["feature"],
        label=humanise_type(r["doc_type"]),
        last_activity=r["last_activity"],
        document_id=r["document_id"],
        badge=badge_kind(r["doc_type"]),
    )


def _feature_card(r: sqlite3.Row, *, label: str, badge: str) -> InboxCard:
    return InboxCard(
        project=r["project"],
        feature=r["feature"],
        label=label,
        last_activity=r["last_activity"],
        badge=badge,
    )


def _shipped_card(r: sqlite3.Row) -> InboxCard:
    return InboxCard(
        project=r["project"],
        feature=r["slug"],
        label="Shipped",
        last_activity=r["shipped_at"],
        badge="shipped",
    )


_CONTENT_EVENTS = frozenset({"created", "updated", "reactivated"})
_COMMENT_EVENTS = frozenset({"comment_submitted", "comment_integrated"})


def classify_reason(
    conn: sqlite3.Connection, document_id: int, doc_type: str, last_read: str | None
) -> InboxReason | None:
    """Classify why a doc re-surfaced in the inbox.

    Returns the reason, or None if no qualifying events exist beyond the baseline.
    """
    baseline = last_read or ""
    rows = conn.execute(
        "SELECT event_type FROM events WHERE document_id = ? AND created_at > ?",
        (document_id, baseline),
    ).fetchall()
    if not rows:
        return None

    event_types = {r["event_type"] for r in rows}

    if event_types & _CONTENT_EVENTS:
        prior = content_at_or_before(conn, document_id, baseline)
        if prior is None:
            return InboxReason(kind="new", label="New")
        curr = current_content(conn, document_id)
        if curr is None:
            return InboxReason(kind="new", label="New")
        doc_diff = diff_contents(prior, curr)
        if doc_diff.changed_count == 0:
            return InboxReason(
                kind="content", label="Updated (formatting only)", changed_count=0, has_diff=False
            )
        manifest = manifest_for(doc_type)
        labels_map = dict(manifest.section_labels)
        changed = doc_diff.changed_keys
        n = len(changed)
        names = [humanise_section_key(k, labels_map) for k in changed[:2]]
        if n <= 2:
            label = "Updated — " + ", ".join(names)
        else:
            label = "Updated — " + ", ".join(names) + f" +{n - 2} more"
        return InboxReason(kind="content", label=label, changed_count=n, has_diff=True)

    if event_types & _COMMENT_EVENTS:
        return InboxReason(kind="comments", label="Comments added")

    return None


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
        "WHERE d.status = 'active' AND f.status IS NOT 'archived' AND EXISTS ("
        "  SELECT 1 FROM events e WHERE e.document_id = d.id "
        "  AND e.created_at > COALESCE("
        "    (SELECT last_read_at FROM read_state WHERE document_id = d.id), '')) "
        "AND NOT (d.type LIKE ? AND NOT EXISTS ("
        "  SELECT 1 FROM synthesis_responses sr WHERE sr.document_id = d.id))"
    )
    params: list[object] = [f"%{FEEDBACK_SUFFIX}"]
    if project_id is not None:
        sql += " AND d.project_id = ?"  # noqa: S608
        params.append(project_id)
    # document_id is a stable secondary key so ties on last_activity have a deterministic order.
    sql += " ORDER BY last_activity DESC, document_id DESC"
    cards = []
    for r in conn.execute(sql, params).fetchall():
        card = _doc_card(r)
        if card.document_id is not None:
            read_ts = last_read_at(conn, card.document_id)
            reason = classify_reason(conn, card.document_id, r["doc_type"], read_ts)
            href = (
                f"/doc/{card.document_id}?view=diff"
                if reason is not None and reason.has_diff
                else f"/doc/{card.document_id}"
            )
            card = InboxCard(
                project=card.project,
                feature=card.feature,
                label=card.label,
                last_activity=card.last_activity,
                document_id=card.document_id,
                badge=card.badge,
                reason=reason,
                href=href,
            )
        cards.append(card)
    return cards


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
    return [
        _feature_card(r, label="In progress", badge="in-progress")
        for r in conn.execute(sql, params).fetchall()
    ]


def parked(conn: sqlite3.Connection, project_id: int | None = None) -> list[InboxCard]:
    sql = (
        "SELECT p.name AS project, f.slug AS feature, "
        "  (SELECT MAX(e.created_at) FROM events e "
        "   JOIN documents d ON e.document_id = d.id "
        "   WHERE d.feature_id = f.id AND d.status='active') AS last_activity "
        "FROM features f JOIN projects p ON f.project_id = p.id "
        "WHERE f.status = 'parked'"
    )
    params: list[object] = []
    if project_id is not None:
        sql += " AND f.project_id = ?"  # noqa: S608
        params.append(project_id)
    sql += " ORDER BY COALESCE(last_activity,'') DESC, f.slug"
    return [
        _feature_card(r, label="Parked", badge="parked")
        for r in conn.execute(sql, params).fetchall()
    ]


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


def awaiting_input(conn: sqlite3.Connection, project_id: int | None = None) -> list[InboxCard]:
    sql = (
        "SELECT d.id AS document_id, d.type AS doc_type, p.name AS project, f.slug AS feature, "
        "  (SELECT MAX(e.created_at) FROM events e WHERE e.document_id = d.id) AS last_activity "
        "FROM documents d "
        "JOIN projects p ON d.project_id = p.id "
        "JOIN features  f ON d.feature_id = f.id "
        "WHERE d.status = 'active' AND f.status IS NOT 'archived' AND d.type LIKE ? "
        "  AND NOT EXISTS (SELECT 1 FROM synthesis_responses sr WHERE sr.document_id = d.id)"
    )
    params: list[object] = [f"%{FEEDBACK_SUFFIX}"]
    if project_id is not None:
        sql += " AND d.project_id = ?"  # noqa: S608
        params.append(project_id)
    sql += " ORDER BY last_activity DESC, document_id DESC"
    return [_doc_card(r) for r in conn.execute(sql, params).fetchall()]


def mark_new_since_read(conn: sqlite3.Connection, project_id: int | None = None) -> int:
    ids = [c.document_id for c in new_since_last_visit(conn, project_id) if c.document_id]
    return mark_documents_read(conn, ids)


def build_inbox(conn: sqlite3.Connection, project: str | None = None) -> Inbox:
    project_id: int | None = None
    if project is not None:
        row = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()
        if row is None:
            return Inbox([], [], [], [], [])
        project_id = row["id"]
    return Inbox(
        new_since=new_since_last_visit(conn, project_id),
        in_progress=in_progress(conn, project_id),
        parked=parked(conn, project_id),
        recently_shipped=recently_shipped(conn, project),
        awaiting_input=awaiting_input(conn, project_id),
    )
