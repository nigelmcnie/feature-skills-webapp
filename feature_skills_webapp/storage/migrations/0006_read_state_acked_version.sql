ALTER TABLE read_state ADD COLUMN acked_version INTEGER;

UPDATE read_state SET acked_version = (
  SELECT MAX(dv.version_num) FROM document_versions dv
  WHERE dv.document_id = read_state.document_id
    AND dv.created_at <= read_state.last_read_at
);

INSERT INTO schema_version (version) VALUES (6)
