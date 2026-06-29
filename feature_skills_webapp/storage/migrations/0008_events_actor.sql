-- Record who originated each event so the inbox surfaces only agent-driven
-- activity, not the developer's own actions (e.g. submitting comments).
-- Coarse two-value enum, 'agent' or 'user'. New rows default to 'agent', the
-- common case. The only user-originated event written today is comment_submitted.
-- (Keep comments free of semicolons -- the migration runner splits on them.)
ALTER TABLE events ADD COLUMN actor TEXT NOT NULL DEFAULT 'agent';

-- Backfill: existing comment_submitted rows are the developer's own comments.
-- Everything else stays agent-originated and keeps the default.
UPDATE events SET actor = 'user' WHERE event_type = 'comment_submitted';

INSERT INTO schema_version (version) VALUES (8)
