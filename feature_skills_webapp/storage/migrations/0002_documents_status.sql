DROP TABLE documents;

CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    feature_id INTEGER REFERENCES features(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source_path TEXT,
    content_html TEXT,
    metadata_json TEXT,
    source_mtime TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_documents_project ON documents(project_id);
CREATE INDEX idx_documents_feature ON documents(feature_id);
CREATE UNIQUE INDEX idx_documents_source_path
    ON documents(source_path) WHERE source_path IS NOT NULL;

INSERT INTO schema_version (version) VALUES (2);
