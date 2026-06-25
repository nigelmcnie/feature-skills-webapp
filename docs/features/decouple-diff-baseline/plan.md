# decouple-diff-baseline — Plan

## Overview

Add a per-document **acknowledged-version** marker to `read_state` so the diff baseline no longer rides on `last_read_at`. The diff compares the current content against the content of the acknowledged version; the marker advances only when the diff is viewed (and, for brand-new single-version docs, on a plain read so they still clear). A single predicate — `latest_version > COALESCE(acked_version, 0)` — defines "has unreviewed changes" and is consumed by the diff view, the inbox surfacing query, the inbox card label, and the plain-view banner, so they move in lockstep. Delivered as one MR with behaviour-pinning tests.

## Key decisions

### Single predicate, defined once

The rule "this document has unreviewed changes" is `latest_version > COALESCE(acked_version, 0)` (version numbers start at 1, so a never-acknowledged doc with any version is unreviewed). It is needed both in Python (diff view, banner, card label) and in SQL (inbox surfacing). Define it once in `read_state.py`:

```
# Python consumers
def has_unreviewed_changes(conn: sqlite3.Connection, document_id: int) -> bool: ...

# SQL consumers: correlated fragment, requires the documents table aliased as `d`
UNREVIEWED_CHANGES_SQL = (
    "(SELECT MAX(version_num) FROM document_versions WHERE document_id = d.id)"
    " > COALESCE((SELECT acked_version FROM read_state WHERE document_id = d.id), 0)"
)
```

Both express the same comparison; the SQL fragment is interpolated into the inbox query (constant string, not user input — keep the existing `noqa: S608` convention).

### Marker advances on diff view; on plain view only for brand-new docs

Viewing the diff (`?view=diff`) advances `acked_version` to the current latest on *any* render outcome (real diff, formatting-only, or no-prior). A *plain* view advances it **only when the doc has a single version** (`latest == 1`) — so a genuinely new doc still clears on read (no pointless "View changes" click; decision confirmed in plan review), while an updated doc (`latest > 1`) stays unreviewed through any number of plain views until its diff is seen. This is the one subtle behavioural rule; it is what makes the reported bug path (plain-view of an update via a relayed URL) keep surfacing.

### Diff baseline by version, null = today's no-prior path

Baseline is the acknowledged version's content. A null marker yields no prior, reusing today's "No earlier version found — nothing to compare" native render unchanged (no synthetic all-inserted diff). The existing timestamp accessor `content_at_or_before` is retained — it still serves the comment/feedback path.

### Inbox surfacing is additive

The new version predicate is added as an `OR` alongside the existing "event newer than `last_read_at`" gate. **Parenthesise carefully** so the status filters and feedback exclusion are untouched — the two surfacing predicates become one OR-group, kept as a separate conjunct:

```
WHERE d.status='active' AND f.status IS NOT 'archived'
  AND ( EXISTS(<event > last_read>) OR <UNREVIEWED_CHANGES_SQL> )
  AND NOT (<feedback exclusion>)
```

So scoped, it can only *add* docs (those plain-read after an update, where the event gate no longer fires but the version gate does) — never remove any, and never double-surface (one row per `d.id`; OR-ing predicates on the same row cannot duplicate it). The feedback exclusion stays its own `AND NOT` conjunct, so a correctly-parenthesised OR cannot resurface feedback docs. The card-label classifier (`classify_reason`) is reworked to derive the reason from the version state rather than from events-newer-than-baseline; the events query is **retained** for the "Comments added" fallback.

### Migration backfill

Existing `read_state` rows get `acked_version` backfilled from the version at-or-before their current `last_read_at` (reproducing today's diff behaviour); rows with no such version stay null.

## Data model

**Table:** `read_state` gains one nullable column.

```
read_state (
  document_id  INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  last_read_at TEXT NOT NULL,
  acked_version INTEGER          -- NEW: version_num whose diff was last viewed; NULL = never
)
```

- `acked_version` references a `document_versions.version_num` for the same document (logical, not a DB FK); always ≤ latest.
- "Latest" is read live as `MAX(version_num)` from `document_versions` — no stored "latest" column.
- New rows created by `mark_read` leave `acked_version` NULL (the upsert does not set it).
- The collision merge (`_merge_read_state`) must carry the column on **both** the INSERT and the conflict path: select the loser's `acked_version` and include it in the INSERT column list, and on conflict keep the greater value treating NULL as 0 — `acked_version = NULLIF(MAX(COALESCE(read_state.acked_version,0), COALESCE(excluded.acked_version,0)), 0)`. (If only the conflict clause is added, a survivor with no prior `read_state` row silently drops the loser's marker on the INSERT branch.)

## Contract

No new endpoints. One behavioural change to the agent-facing write/read responses in `web/submit.py`: the returned `url` points at the diff view when the document already has a prior version, so a relayed "go look" link opens on the change.

- `put_document`: `url = f"/doc/{id}?view=diff" if not result.created else f"/doc/{id}"` (a create returns the plain URL; an update returns the diff URL — keyed off `created`, not `changed`, so a byte-identical resubmit still returns the diff URL, which falls back gracefully to "no changes").
- `get_document`: `url = f"/doc/{id}?view=diff" if ver_row["ver"] > 1 else f"/doc/{id}"`.

The diff view itself is the existing `GET /doc/{id}?view=diff` render mode — unchanged in shape, only its baseline source moves.

## File structure

### New

- `feature_skills_webapp/storage/migrations/0006_read_state_acked_version.sql` — add column + backfill + bump schema_version to 6.

### Modified — storage

- `storage/read_state.py` — `acked_version()` reader, `mark_diff_seen()` writer, `has_unreviewed_changes()` predicate, `UNREVIEWED_CHANGES_SQL` fragment.
- `storage/versions.py` — `content_at_version()` accessor; extend `_merge_read_state()` to carry `acked_version` (INSERT + conflict).
- `storage/inbox.py` — add the parenthesised `UNREVIEWED_CHANGES_SQL` OR-clause to `new_since_last_visit`; rework `classify_reason` to derive the reason from version state (events query retained for the comment fallback).

### Modified — web

- `web/doc_view.py` — baseline from `acked_version`; fetch `latest = MAX(version_num)` in the section branch; advance marker (diff view always; native only when `latest==1`); pass banner flag.
- `web/templates/doc.html` — non-blocking "changed since you reviewed" banner on native section docs.
- `web/submit.py` — diff-aware `url` in `put_document`/`get_document`.

### Tests

- `storage/read_state_test.py`, `storage/versions_test.py`, `storage/inbox_test.py`, `storage/db_test.py` (migration + bump the four hardcoded `==5` assertions), `web/doc_view_test.py`, `web/submit_test.py` — review existing coverage and extend per the QC list.

## Verification

Run from the repo root; all must pass (these are the CLAUDE.md QC gates):

```
uv run ruff format --check .
uv run ruff check .
uv run ty check .
uv run pytest
```

Behaviour-pinning (each must go red when its production change is reverted; name new tests with these tokens so the filter catches them):

```
# the original regression: plain view then diff still shows the change
uv run pytest -k "acked or diff_baseline or unreviewed or surfac" -q
```

Note: the `-k` filter is a convenience — today it matches one pre-existing test, so a green run does *not* prove the new tests exist. The real gate is the "red when reverted" discipline per test.

Migration applies on next process start (or a fresh DB); for the live systemd service, after install it needs a restart per CLAUDE.md — `systemctl --user restart feature-skills-webapp` (code-only change, no reinstall needed) — then the `schema_version` table shows 6.

## Qc

Follow the QC steps in `CLAUDE.md` at implementation time (ruff format, ruff check, ty check, pytest — xdist + pytest-socket, per-worker DB). Tests to review/extend (each must be able to fail without the change):

- **read_state_test:** `acked_version` read (null + set); `mark_diff_seen` sets to current latest and is idempotent; `has_unreviewed_changes` truth table (null/equal/less/greater).
- **versions_test:** `content_at_version` returns the right version's content, None for null/absent; `_merge_read_state` keeps the greater `acked_version` — covering the both-present case, the one-side-null case, and the **survivor-absent** case (survivor had no `read_state` row, so the merge takes the INSERT branch and must still carry the loser's marker).
- **doc_view_test:** plain view then `?view=diff` still shows the change; diff view advances the marker (re-request → "no changes"); marker advances on formatting-only and no-prior diff views; two updates between reviews → diff spans from acked to latest; new single-version doc clears on plain read (latest==1 advance); updated doc shows the banner on native view, new doc does not.
- **inbox_test:** a plain-read-after-update section doc still surfaces (through `new_since_last_visit`, not just the label); viewing the diff clears it; `classify_reason` labels a version-only-surfaced doc; comment/feedback surfacing unchanged; the `extra_css_used`-only doc still classifies None; no doc surfaces twice.
- **db_test (migration):** 0006 adds the column and backfills `acked_version` from version-at-`last_read_at`; a read-but-not-diffed existing doc keeps a sensible prior (not wholly new); schema_version reaches 6. **Bump the four existing hardcoded `==5` assertions to `==6`** (`test_migrate_fresh_returns_version_5`, `test_migrate_idempotent`, `test_schema_version_after_migrate`, `test_migrate_v1_to_v2_upgrade_path`) — rename the first if convenient.
- **submit_test:** create → plain url; update → `?view=diff` url, for both `put_document` and `get_document`; a byte-identical resubmit (created=False) still returns the diff url (intentional).

## Checklist

### Single phase

- Add migration `0006_read_state_acked_version.sql`: ALTER TABLE add `acked_version`, backfill from version-at-`last_read_at`, bump schema_version to 6.
- read_state.py: add `acked_version()`, `mark_diff_seen()`, `has_unreviewed_changes()`, and the `UNREVIEWED_CHANGES_SQL` fragment.
- versions.py: add `content_at_version()`; extend `_merge_read_state()` to carry `acked_version` on both INSERT and conflict paths (greater, null-as-0).
- doc_view.py: diff branch baselines off `acked_version` via `content_at_version`; advance marker with `mark_diff_seen` after the mode decision.
- doc_view.py: fetch `latest = MAX(version_num)` in the native section branch; advance marker only when `latest==1`; otherwise set the `unreviewed_banner` flag from `has_unreviewed_changes`.
- doc.html: render the non-blocking `.diff-note` banner with a View-changes link when `unreviewed_banner` is set.
- inbox.py: add the parenthesised `( EXISTS(event>last_read) OR UNREVIEWED_CHANGES_SQL )` OR-group to `new_since_last_visit`, leaving status filters and the feedback `AND NOT` conjunct intact.
- inbox.py: rework `classify_reason` to derive the reason from version state (New / Updated / formatting-only), retaining the events query for the comment fallback.
- submit.py: return a `?view=diff` url for updates in `put_document` and `get_document`.
- Review and extend tests across read_state, versions, doc_view, inbox, submit per the QC list; confirm each new behavioural test fails when its production change is reverted.
- db_test.py: bump the four hardcoded `==5` schema-version assertions to `==6`; add the 0006 backfill/migration tests.
- Run the full QC gate (ruff format/check, ty, pytest); all green.

## Single phase

One MR. Order of work:

#### 1. Migration

```
-- 0006_read_state_acked_version.sql  (statements split naively on ';' — plain DDL/DML only)
ALTER TABLE read_state ADD COLUMN acked_version INTEGER;

UPDATE read_state SET acked_version = (
  SELECT MAX(dv.version_num) FROM document_versions dv
  WHERE dv.document_id = read_state.document_id
    AND dv.created_at <= read_state.last_read_at
);

INSERT INTO schema_version (version) VALUES (6)
```

#### 2. Storage accessors + predicate (read_state.py, versions.py)

```
def acked_version(conn, document_id: int) -> int | None
def mark_diff_seen(conn, document_id: int) -> None      # acked = MAX(version_num) for doc; own transaction; idempotent
def has_unreviewed_changes(conn, document_id: int) -> bool
# versions.py
def content_at_version(conn, document_id: int, version_num: int | None) -> ParsedContent | None  # None if version_num is None or row absent
```

#### 3. Diff view + banner (doc_view.py, doc.html)

In the `view == "diff"` branch: `acked = acked_version(conn, doc_id); prior = content_at_version(conn, doc_id, acked)`. Keep the existing prior-None → "nothing to compare" and no-textual-change → note branches. After the mode is decided, call `mark_diff_seen(conn, doc_id)` (in addition to the existing `mark_read`). In the native section-doc branch: fetch `latest = MAX(version_num)` for the doc (it is *not* currently in scope there — `current_content` doesn't return it); if `latest == 1`, call `mark_diff_seen` and show no banner; else set `unreviewed_banner = has_unreviewed_changes(conn, doc_id)` and pass it to the template. Banner markup (contract `.diff-note`): `<p class="diff-note">This document changed since you last reviewed it. <a href="/doc/{{ doc_id }}?view=diff">View changes</a></p>`.

#### 4. Inbox (inbox.py)

In `new_since_last_visit`, replace the bare event-gate with the parenthesised OR-group shown in Key technical decisions: `AND ( EXISTS(<event > last_read>) OR <UNREVIEWED_CHANGES_SQL> )`, leaving the `d.status`/`f.status` filters and the trailing `AND NOT (feedback…)` conjunct intact (documents aliased `d`). Rework `classify_reason`: compute `acked`, `prior = content_at_version(acked)`, `latest`; if `latest > COALESCE(acked,0)` → prior None ⇒ "New", else diff prior vs current ⇒ "Updated — …"/"formatting only" (reuse existing `humanise_section_key` logic); else fall through to comment events ("Comments added") keyed off `last_read_at` — the existing events query stays for this branch; else None. Confirm `test_classify_reason_extra_css_used_event_is_not_surfaced` still passes (an `extra_css_used`-only doc with no unreviewed change must classify None).

#### 5. Collision merge (versions.py)

Extend `_merge_read_state` to read the loser's `acked_version` and carry it on both the INSERT column list and the DO-UPDATE clause, per the Data-model expression.

#### 6. Agent URL (submit.py)

Diff-aware `url` per the HTTP contract section.

#### 7. Tests

Review existing coverage in the listed files and extend per QC, including bumping the existing schema-version assertions.
