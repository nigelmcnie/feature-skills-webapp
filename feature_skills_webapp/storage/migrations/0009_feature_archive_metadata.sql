-- Semantic feature archival metadata. All nullable: populated on archive,
-- cleared on unarchive. Pre-existing archived rows (dropped before this
-- migration) legitimately carry NULL and must render as blank.
ALTER TABLE features ADD COLUMN archive_reason TEXT;
ALTER TABLE features ADD COLUMN superseded_by TEXT;
ALTER TABLE features ADD COLUMN archive_note TEXT;
ALTER TABLE features ADD COLUMN archived_at TEXT;

INSERT INTO schema_version (version) VALUES (9)
