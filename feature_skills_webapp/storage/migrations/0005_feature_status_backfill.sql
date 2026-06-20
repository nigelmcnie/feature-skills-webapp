UPDATE features SET status = 'available' WHERE status IS NULL;

INSERT INTO schema_version (version) VALUES (5)
