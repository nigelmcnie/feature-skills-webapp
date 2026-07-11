-- Add archival metadata to documents so API-authored documents can be
-- retired and later reactivated. Nullable everywhere: NULL for every active
-- document, populated on archive, cleared on unarchive.
-- (Keep comments free of semicolons -- the migration runner splits on them.)
ALTER TABLE documents ADD COLUMN archive_reason TEXT;
ALTER TABLE documents ADD COLUMN superseded_by TEXT;
ALTER TABLE documents ADD COLUMN archive_note TEXT;
ALTER TABLE documents ADD COLUMN archived_at TEXT;

INSERT INTO schema_version (version) VALUES (10)
