"""Read model for retro findings — used by the project page panel."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class FindingRow:
    id: int
    title: str
    evidence: str | None
    change: str | None
    status: str
    feature: str | None
    recurs_from: int | None
    recurrence_count: int
    created_at: str


def list_findings(conn: sqlite3.Connection, project_id: int) -> list[FindingRow]:
    """Return all findings for a project in panel display order.

    Recurring findings (recurrence_count > 0) sort first, then oldest by
    created_at, with id as a deterministic tie-break.
    """
    rows = conn.execute(
        "SELECT f.id, f.title, f.evidence, f.change, f.status, f.feature, "
        "f.recurs_from, f.created_at, "
        "(SELECT COUNT(*) FROM retro_findings c WHERE c.recurs_from = f.id) "
        "AS recurrence_count "
        "FROM retro_findings f "
        "WHERE f.project_id = ? "
        "ORDER BY recurrence_count DESC, f.created_at ASC, f.id ASC",
        (project_id,),
    ).fetchall()
    return [
        FindingRow(
            id=r["id"],
            title=r["title"],
            evidence=r["evidence"],
            change=r["change"],
            status=r["status"],
            feature=r["feature"],
            recurs_from=r["recurs_from"],
            recurrence_count=r["recurrence_count"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
