CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    repo_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    status TEXT,
    owner TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, slug)
);

CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id INTEGER NOT NULL REFERENCES features(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    source_path TEXT,
    content_html TEXT,
    metadata_json TEXT,
    source_mtime TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE read_state (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    last_read_at TEXT NOT NULL
);

CREATE TABLE synthesis_responses (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    item_num INTEGER NOT NULL,
    response TEXT,
    routine_flag TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (document_id, item_num)
);

CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    excerpt TEXT,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    integrated_at TEXT
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- SET NULL, not CASCADE: events is an append-only audit log, so the
    -- history must survive a document being deleted (it's a satellite,
    -- not part of the projects→features→documents cascade spine).
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

-- FK indexes (the inbox/activity queries traverse these).
CREATE INDEX idx_features_project ON features(project_id);
CREATE INDEX idx_documents_feature ON documents(feature_id);
CREATE INDEX idx_events_document ON events(document_id);

CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version (version) VALUES (1)
