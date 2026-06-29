"""Agent-submission storage core: create-or-update documents by logical identity."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from feature_skills_webapp.storage.doc_content import (
    ParsedContent,
    Section,
    manifest_for,
    serialise,
)
from feature_skills_webapp.storage.doc_render import css_has_brace_error, css_has_style_breakout
from feature_skills_webapp.storage.events import ACTOR_AGENT
from feature_skills_webapp.storage.inbox import humanise_type
from feature_skills_webapp.storage.parents import logical_key
from feature_skills_webapp.storage.tracker import require_feature, require_project
from feature_skills_webapp.storage.versions import current_content, record_version

WRITABLE_SECTION_TYPES = frozenset({"context", "requirements", "plan"})
MAX_BODY_BYTES = 1024 * 1024  # 1 MB


class SubmitError(Exception):
    """Validation failure — surfaced by the web layer as 400."""


@dataclass(frozen=True)
class SubmitResult:
    document_id: int
    logical_key: str
    version_num: int
    created: bool
    changed: bool  # False when content was byte-identical (no new version cut)
    event_type: str | None = field(default=None)


def validate_writable(doc_type: str, feature: str | None, instance: int) -> None:
    """Raise SubmitError unless this identity is writable in v1.

    Writable: doc_type in WRITABLE_SECTION_TYPES or ends with '-feedback';
    feature-scoped only; instance must be 1 unless doc_type ends with '-feedback'.
    """
    if doc_type not in WRITABLE_SECTION_TYPES and not doc_type.endswith("-feedback"):
        raise SubmitError(
            f"doc_type {doc_type!r} is not writable"
            f" (v1 supports: context, requirements, plan, *-feedback)"
        )
    if feature is None:
        raise SubmitError("feature must be specified (project-level docs are not writable)")
    if instance != 1 and not doc_type.endswith("-feedback"):
        raise SubmitError(f"instance must be 1 for {doc_type!r} (got {instance})")


def build_content(
    doc_type: str,
    sections: dict[str, str] | None,
    body: str | None,
    extra_css: str | None = None,
) -> ParsedContent:
    """Validate against manifest_for(doc_type) and build ParsedContent, else SubmitError.

    Opaque docs: require `body`, forbid `sections` and `extra_css`.
    Section docs: require `sections`, forbid `body`; reject unknown keys; enforce MAX_BODY_BYTES.
    Sections are stored in manifest order (fixed keys first, then repeated-prefix keys sorted).
    extra_css: whitespace-only normalises to ""; size-bounded by MAX_BODY_BYTES.
    """
    spec = manifest_for(doc_type)

    # Normalise extra_css: absent/whitespace-only → ""
    normalised_css = (extra_css or "").strip()

    if spec.shape == "opaque":
        if sections is not None:
            raise SubmitError("'sections' is not accepted for opaque doc types")
        if body is None:
            raise SubmitError("'body' is required for opaque doc types")
        if len(body.encode()) > MAX_BODY_BYTES:
            raise SubmitError("'body' exceeds 1 MB")
        if normalised_css:
            raise SubmitError("'extra_css' is not accepted for opaque doc types")
        return ParsedContent(shape="opaque", sections=(Section(key="", body=body),))

    # sections shape
    if body is not None:
        raise SubmitError("'body' is not accepted for section doc types")
    if sections is None:
        raise SubmitError("'sections' is required for section doc types")
    if not isinstance(sections, dict):
        raise SubmitError("'sections' must be an object")

    valid_fixed = set(spec.expected_keys)
    for key in sections:
        is_fixed = key in valid_fixed
        is_repeated = any(key.startswith(p) for p in spec.repeated_prefixes)
        if not is_fixed and not is_repeated:
            raise SubmitError(f"unknown section key {key!r}")

    for key, val in sections.items():
        if not isinstance(val, str):
            raise SubmitError(f"section value for {key!r} must be a string")
        if len(val.encode()) > MAX_BODY_BYTES:
            raise SubmitError(f"section {key!r} exceeds 1 MB")

    if normalised_css and len(normalised_css.encode()) > MAX_BODY_BYTES:
        raise SubmitError("'extra_css' exceeds 1 MB")

    if normalised_css and css_has_brace_error(normalised_css):
        raise SubmitError(
            "'extra_css' has an unmatched '}' — a stray closing brace would let it "
            "break out of the scoped style block and affect the page chrome"
        )

    if normalised_css and css_has_style_breakout(normalised_css):
        raise SubmitError(
            "'extra_css' must not contain '</style>' or '<!--' — either would break "
            "out of the scoped style block and inject markup into the page chrome"
        )

    # Build in manifest order: fixed keys first (present only), then sorted repeated-prefix keys
    built: list[Section] = []
    for k in spec.expected_keys:
        if k in sections:
            built.append(Section(key=k, body=sections[k]))
    for key in sorted(k for k in sections if any(k.startswith(p) for p in spec.repeated_prefixes)):
        built.append(Section(key=key, body=sections[key]))

    return ParsedContent(shape="sections", sections=tuple(built), extra_css=normalised_css)


def _event_payload(doc_type: str, feature: str | None, source_path: str | None) -> str:
    d: dict[str, object] = {"type": doc_type, "feature": feature}
    if source_path is not None:
        d["path"] = source_path
    return json.dumps(d)


def submit_document(
    conn: sqlite3.Connection,
    *,
    project: str,
    feature: str | None,
    doc_type: str,
    instance: int,
    content: ParsedContent,
    actor: str = "agent",
    now: str,
    # Importer-only params — when source_path is provided, walker semantics apply.
    source_path: str | None = None,
    source_mtime: str | None = None,
    doc_status: str = "active",
    doc_size: int | None = None,
    doc_title: str | None = None,
) -> SubmitResult:
    """Create-or-update a document by logical_key. Caller wraps in transaction().

    API mode (source_path=None):
    - INSERT: create row (status='active'), record_version (v1), emit 'created' event.
    - UPDATE existing: update project/feature linkage, record_version + 'updated' event
      on change; identical content → no version, no event (changed=False).
    - UPDATE no-version (cur is None): seed v1 silently, no event.

    Importer mode (source_path provided):
    - Same INSERT/UPDATE logic but with source_path, source_mtime, doc_status, doc_size,
      doc_title stored on the document row.
    - Event type on content change: 'reactivated' (if was missing), 'archived'
      (transitioning to archived), or 'updated' (all other cases).
    - Caller must declare parents (upsert_project/upsert_feature) before calling —
      require_project/require_feature will find them.
    """
    lkey = logical_key(project, feature, doc_type, instance)

    project_id = require_project(conn, project)
    feature_id = require_feature(conn, project_id, feature) if feature is not None else None

    row = conn.execute(
        "SELECT id, status, metadata_json FROM documents WHERE logical_key=?",
        (lkey,),
    ).fetchone()

    if row is None:
        if source_path is not None:
            title = doc_title or (
                f"{feature} — {humanise_type(doc_type)}" if feature else humanise_type(doc_type)
            )
            meta_dict: dict[str, object] = {"title": title}
            if doc_size is not None:
                meta_dict["size"] = doc_size
            cursor = conn.execute(
                "INSERT INTO documents "
                "(project_id, feature_id, type, status, source_path, logical_key, instance, "
                "metadata_json, source_mtime, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    feature_id,
                    doc_type,
                    doc_status,
                    source_path,
                    lkey,
                    instance,
                    json.dumps(meta_dict),
                    source_mtime,
                    now,
                    now,
                ),
            )
        else:
            title = f"{feature} — {humanise_type(doc_type)}" if feature else humanise_type(doc_type)
            cursor = conn.execute(
                "INSERT INTO documents "
                "(project_id, feature_id, type, status, source_path, logical_key, instance, "
                "metadata_json, source_mtime, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', NULL, ?, ?, ?, NULL, ?, ?)",
                (
                    project_id,
                    feature_id,
                    doc_type,
                    lkey,
                    instance,
                    json.dumps({"title": title}),
                    now,
                    now,
                ),
            )
        doc_id = cursor.lastrowid
        assert doc_id is not None
        ver_num = record_version(conn, doc_id, content, actor=actor, now=now)
        conn.execute(
            "INSERT INTO events (document_id, event_type, payload_json, created_at, actor) "
            "VALUES (?, 'created', ?, ?, ?)",
            (doc_id, _event_payload(doc_type, feature, source_path), now, ACTOR_AGENT),
        )
        if content.extra_css:
            conn.execute(
                "INSERT INTO events (document_id, event_type, payload_json, created_at, actor) "
                "VALUES (?, 'extra_css_used', ?, ?, ?)",
                (doc_id, json.dumps({"type": doc_type, "feature": feature}), now, ACTOR_AGENT),
            )
        return SubmitResult(
            document_id=doc_id,
            logical_key=lkey,
            version_num=ver_num,
            created=True,
            changed=True,
            event_type="created",
        )

    doc_id = row["id"]
    old_status = row["status"]

    if source_path is not None:
        meta_dict = json.loads(row["metadata_json"] or "{}")
        if doc_title is not None:
            meta_dict["title"] = doc_title
        if doc_size is not None:
            meta_dict["size"] = doc_size
        conn.execute(
            "UPDATE documents SET project_id=?, feature_id=?, type=?, status=?, "
            "source_path=?, source_mtime=?, metadata_json=?, updated_at=? WHERE id=?",
            (
                project_id,
                feature_id,
                doc_type,
                doc_status,
                source_path,
                source_mtime,
                json.dumps(meta_dict),
                now,
                doc_id,
            ),
        )
    else:
        conn.execute(
            "UPDATE documents SET project_id=?, feature_id=?, updated_at=? WHERE id=?",
            (project_id, feature_id, now, doc_id),
        )

    cur = current_content(conn, doc_id)

    if cur is None:
        # Seed the first version silently — no event, no summary counter.
        ver_num = record_version(conn, doc_id, content, actor=actor, now=now)
        return SubmitResult(
            document_id=doc_id,
            logical_key=lkey,
            version_num=ver_num,
            created=False,
            changed=True,
            event_type=None,
        )

    if serialise(cur) != serialise(content):
        ver_num = record_version(conn, doc_id, content, actor=actor, now=now)
        if source_path is not None:
            # Importer event routing: reactivation wins over archival.
            if old_status == "missing":
                event_type: str = "reactivated"
            elif doc_status == "archived" and old_status != "archived":
                event_type = "archived"
            else:
                event_type = "updated"
        else:
            event_type = "updated"
        conn.execute(
            "INSERT INTO events (document_id, event_type, payload_json, created_at, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc_id, event_type, _event_payload(doc_type, feature, source_path), now, ACTOR_AGENT),
        )
        if content.extra_css:
            conn.execute(
                "INSERT INTO events (document_id, event_type, payload_json, created_at, actor) "
                "VALUES (?, 'extra_css_used', ?, ?, ?)",
                (doc_id, json.dumps({"type": doc_type, "feature": feature}), now, ACTOR_AGENT),
            )
        return SubmitResult(
            document_id=doc_id,
            logical_key=lkey,
            version_num=ver_num,
            created=False,
            changed=True,
            event_type=event_type,
        )

    # Identical content — no new version, no event
    ver_row = conn.execute(
        "SELECT COALESCE(MAX(version_num), 0) AS ver FROM document_versions WHERE document_id=?",
        (doc_id,),
    ).fetchone()
    return SubmitResult(
        document_id=doc_id,
        logical_key=lkey,
        version_num=ver_row["ver"],
        created=False,
        changed=False,
        event_type=None,
    )
