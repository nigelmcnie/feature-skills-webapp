CREATE TABLE retro_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_key     TEXT NOT NULL,
    feature     TEXT,
    ran_at      TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, run_key)
);

CREATE TABLE retro_findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES retro_runs(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    feature     TEXT,
    title       TEXT NOT NULL,
    evidence    TEXT,
    change      TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    recurs_from INTEGER REFERENCES retro_findings(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_retro_runs_project        ON retro_runs(project_id);
CREATE INDEX idx_retro_findings_project    ON retro_findings(project_id);
CREATE INDEX idx_retro_findings_run        ON retro_findings(run_id);
CREATE INDEX idx_retro_findings_recurs_from ON retro_findings(recurs_from);

INSERT INTO schema_version (version) VALUES (4)