"""Agent-submission storage core: create-or-update documents by logical identity."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from feature_skills_webapp.storage.doc_content import (
    ParsedContent,
    Section,
    manifest_for,
    serialise,
)
from feature_skills_webapp.storage.doc_render import css_has_brace_error, css_has_style_breakout
from feature_skills_webapp.storage.inbox import humanise_type
from feature_skills_webapp.storage.tracker import require_feature, require_project
from feature_skills_webapp.storage.versions import current_content, record_version
from feature_skills_webapp.storage.walker import logical_key

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


def submit_document(
    conn: sqlite3.Connection,
    *,
    project: str,
    feature: str | None,
    doc_type: str,
    instance: int,
    content: ParsedContent,
    actor: str,
    now: str,
) -> SubmitResult:
    """Create-or-update a document by logical_key. Caller wraps in transaction().

    Three write states mirroring walker._process_file:
    - INSERT: create row, record_version (v1), emit 'created' event.
    - Existing row with current version: record_version + 'updated' event only on change;
      identical content → no version, no event (changed=False).
    - Existing row with no version (cur is None): seed v1 silently, no event.
    """
    lkey = logical_key(project, feature, doc_type, instance)

    project_id = require_project(conn, project)
    feature_id = require_feature(conn, project_id, feature) if feature is not None else None

    row = conn.execute(
        "SELECT id FROM documents WHERE logical_key=?",
        (lkey,),
    ).fetchone()

    if row is None:
        title = f"{feature} — {humanise_type(doc_type)}" if feature else humanise_type(doc_type)
        meta = json.dumps({"title": title})
        cursor = conn.execute(
            "INSERT INTO documents "
            "(project_id, feature_id, type, status, source_path, logical_key, instance, "
            "metadata_json, source_mtime, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', NULL, ?, ?, ?, NULL, ?, ?)",
            (project_id, feature_id, doc_type, lkey, instance, meta, now, now),
        )
        doc_id = cursor.lastrowid
        assert doc_id is not None
        ver_num = record_version(conn, doc_id, content, actor=actor, now=now)
        conn.execute(
            "INSERT INTO events (document_id, event_type, payload_json, created_at) "
            "VALUES (?, 'created', ?, ?)",
            (doc_id, json.dumps({"type": doc_type, "feature": feature}), now),
        )
        if content.extra_css:
            conn.execute(
                "INSERT INTO events (document_id, event_type, payload_json, created_at) "
                "VALUES (?, 'extra_css_used', ?, ?)",
                (doc_id, json.dumps({"type": doc_type, "feature": feature}), now),
            )
        return SubmitResult(
            document_id=doc_id,
            logical_key=lkey,
            version_num=ver_num,
            created=True,
            changed=True,
        )

    doc_id = row["id"]
    conn.execute(
        "UPDATE documents SET project_id=?, feature_id=?, updated_at=? WHERE id=?",
        (project_id, feature_id, now, doc_id),
    )

    cur = current_content(conn, doc_id)

    if cur is None:
        # Seed the first version silently — no event, matches walker's cur is None branch
        ver_num = record_version(conn, doc_id, content, actor=actor, now=now)
        return SubmitResult(
            document_id=doc_id,
            logical_key=lkey,
            version_num=ver_num,
            created=False,
            changed=True,
        )

    if serialise(cur) != serialise(content):
        ver_num = record_version(conn, doc_id, content, actor=actor, now=now)
        conn.execute(
            "INSERT INTO events (document_id, event_type, payload_json, created_at) "
            "VALUES (?, 'updated', ?, ?)",
            (doc_id, json.dumps({"type": doc_type, "feature": feature}), now),
        )
        if content.extra_css:
            conn.execute(
                "INSERT INTO events (document_id, event_type, payload_json, created_at) "
                "VALUES (?, 'extra_css_used', ?, ?)",
                (doc_id, json.dumps({"type": doc_type, "feature": feature}), now),
            )
        return SubmitResult(
            document_id=doc_id,
            logical_key=lkey,
            version_num=ver_num,
            created=False,
            changed=True,
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
    )
