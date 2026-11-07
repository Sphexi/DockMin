-- How far each container log exporter rule has copied each container's log.

CREATE TABLE container_log_cursor (
    rule_id            INTEGER NOT NULL REFERENCES automation_rule (id) ON DELETE CASCADE,
    -- Rule ids are reused after a delete, so a row is valid only while this matches the rule.
    rule_created_at    TEXT NOT NULL,
    environment_id     INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    container_id       TEXT NOT NULL CHECK (
                           length(container_id) = 64
                           AND container_id NOT GLOB '*[^0-9a-f]*'
                       ),
    last_timestamp_ns  INTEGER NOT NULL CHECK (last_timestamp_ns >= 0),
    lines_at_last      INTEGER NOT NULL CHECK (lines_at_last >= 1),
    object_path        TEXT CHECK (object_path IS NULL OR length(object_path) BETWEEN 1 AND 1024),
    last_seen_at       TEXT NOT NULL CHECK (
                           length(last_seen_at) = 20
                           AND last_seen_at GLOB
                               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                       ),
    PRIMARY KEY (rule_id, environment_id, container_id)
) WITHOUT ROWID;

CREATE INDEX idx_container_log_cursor_environment_id ON container_log_cursor (environment_id);
