ALTER TABLE projects ADD COLUMN suggested_order TEXT;

INSERT INTO schema_version (version) VALUES (7)
