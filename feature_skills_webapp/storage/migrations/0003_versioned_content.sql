ALTER TABLE documents ADD COLUMN logical_key TEXT;
ALTER TABLE documents ADD COLUMN instance INTEGER NOT NULL DEFAULT 1;

CREATE TABLE document_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_num INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (document_id, version_num)
);
CREATE INDEX idx_document_versions_document ON document_versions(document_id);
CREATE INDEX idx_documents_logical_key ON documents(logical_key);

INSERT INTO schema_version (version) VALUES (3)
