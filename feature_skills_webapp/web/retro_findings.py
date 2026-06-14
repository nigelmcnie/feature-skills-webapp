"""Retro findings capture: write and read endpoint handlers."""

from __future__ import annotations

from typing import Any, cast

from starlette.requests import Request
from starlette.responses import JSONResponse

from feature_skills_webapp.storage.db import now_iso, transaction
from feature_skills_webapp.web.db_dep import request_conn

_MAX_VALUE_BYTES = 1024 * 1024  # 1 MB


def _check_optional_str(value: Any, field: str) -> str | None:
    """Return error message string if value is not a valid optional text field, else None."""
    if value is None:
        return None
    if not isinstance(value, str):
        return f"'{field}' must be a string or null"
    if len(value.encode()) > _MAX_VALUE_BYTES:
        return f"'{field}' exceeds 1 MB"
    return None


async def post_retro_findings(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    project = body.get("project")
    if not isinstance(project, str):
        return JSONResponse({"error": "'project' must be a string"}, status_code=400)

    run = body.get("run")
    if not isinstance(run, dict):
        return JSONResponse({"error": "'run' must be an object"}, status_code=400)

    run_key = run.get("key")
    if not isinstance(run_key, str) or not run_key:
        return JSONResponse({"error": "'run.key' must be a non-empty string"}, status_code=400)

    run_feature = run.get("feature")
    err = _check_optional_str(run_feature, "run.feature")
    if err:
        return JSONResponse({"error": err}, status_code=400)

    run_ran_at = run.get("ran_at")
    err = _check_optional_str(run_ran_at, "run.ran_at")
    if err:
        return JSONResponse({"error": err}, status_code=400)

    findings_raw = body.get("findings")
    if not isinstance(findings_raw, list):
        return JSONResponse({"error": "'findings' must be a list"}, status_code=400)

    validated: list[dict[str, Any]] = []
    for i, item in enumerate(findings_raw):
        if not isinstance(item, dict):
            return JSONResponse({"error": f"findings[{i}] must be an object"}, status_code=400)

        item_d = cast("dict[str, Any]", item)
        title = item_d.get("title")
        if not isinstance(title, str) or not title:
            return JSONResponse(
                {"error": f"findings[{i}].title must be a non-empty string"}, status_code=400
            )
        if len(title.encode()) > _MAX_VALUE_BYTES:
            return JSONResponse({"error": f"findings[{i}].title exceeds 1 MB"}, status_code=400)

        evidence = item_d.get("evidence")
        err = _check_optional_str(evidence, f"findings[{i}].evidence")
        if err:
            return JSONResponse({"error": err}, status_code=400)

        change = item_d.get("change")
        err = _check_optional_str(change, f"findings[{i}].change")
        if err:
            return JSONResponse({"error": err}, status_code=400)

        recurs_from = item_d.get("recurs_from")
        if recurs_from is not None and not isinstance(recurs_from, int):
            return JSONResponse(
                {"error": f"findings[{i}].recurs_from must be an integer or null"},
                status_code=400,
            )

        validated.append(
            {
                "title": title,
                "evidence": evidence,
                "change": change,
                "recurs_from": recurs_from,
            }
        )

    with request_conn(request.app) as conn:
        proj_row = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()
        if proj_row is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        project_id: int = proj_row["id"]

        old_run_row = conn.execute(
            "SELECT id FROM retro_runs WHERE project_id = ? AND run_key = ?",
            (project_id, run_key),
        ).fetchone()
        old_run_id: int | None = old_run_row["id"] if old_run_row else None

        recurs_ids = {f["recurs_from"] for f in validated if f["recurs_from"] is not None}
        for rf_id in recurs_ids:
            rf_row = conn.execute(
                "SELECT id, project_id, run_id FROM retro_findings WHERE id = ?",
                (rf_id,),
            ).fetchone()
            if rf_row is None:
                return JSONResponse(
                    {"error": f"recurs_from {rf_id}: finding not found"}, status_code=400
                )
            if rf_row["project_id"] != project_id:
                return JSONResponse(
                    {"error": f"recurs_from {rf_id}: belongs to a different project"},
                    status_code=400,
                )
            if old_run_id is not None and rf_row["run_id"] == old_run_id:
                return JSONResponse(
                    {"error": f"recurs_from {rf_id}: belongs to the run being replaced"},
                    status_code=400,
                )

        now = now_iso()
        run_id: int = 0
        with transaction(conn):
            conn.execute(
                "DELETE FROM retro_runs WHERE project_id = ? AND run_key = ?",
                (project_id, run_key),
            )
            conn.execute(
                "INSERT INTO retro_runs (project_id, run_key, feature, ran_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, run_key, run_feature, run_ran_at, now),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for f in validated:
                conn.execute(
                    "INSERT INTO retro_findings "
                    "(run_id, project_id, feature, title, evidence, change, status, "
                    "recurs_from, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                    (
                        run_id,
                        project_id,
                        run_feature,
                        f["title"],
                        f["evidence"],
                        f["change"],
                        f["recurs_from"],
                        now,
                        now,
                    ),
                )

    request.app.state.broadcaster.broadcast()
    return JSONResponse({"run_id": run_id, "findings_written": len(validated)})


async def get_retro_findings(request: Request) -> JSONResponse:
    if request.app.state.db_path is None:
        return JSONResponse({"error": "db not configured"}, status_code=503)

    project = request.query_params.get("project")
    if not project:
        return JSONResponse({"error": "project parameter required"}, status_code=400)

    with request_conn(request.app) as conn:
        proj_row = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()
        if proj_row is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        project_id: int = proj_row["id"]

        rows = conn.execute(
            "SELECT f.id, f.title, f.evidence, f.change, f.status, f.feature, "
            "f.recurs_from, f.created_at, "
            "(SELECT COUNT(*) FROM retro_findings c WHERE c.recurs_from = f.id) "
            "AS recurrence_count "
            "FROM retro_findings f "
            "WHERE f.project_id = ? AND f.status IN ('open', 'deferred') "
            "ORDER BY f.created_at, f.id",
            (project_id,),
        ).fetchall()

    findings = [
        {
            "id": r["id"],
            "title": r["title"],
            "evidence": r["evidence"],
            "change": r["change"],
            "status": r["status"],
            "feature": r["feature"],
            "recurs_from": r["recurs_from"],
            "recurrence_count": r["recurrence_count"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return JSONResponse({"project": project, "findings": findings})
