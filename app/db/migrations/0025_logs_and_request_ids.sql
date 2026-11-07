-- activity_event and environment each gain a nullable column; service_log_cursor is created.

ALTER TABLE activity_event ADD COLUMN request_id TEXT CHECK (
    request_id IS NULL OR (
        length(request_id) BETWEEN 1 AND 64
        AND request_id NOT GLOB '*[^A-Za-z0-9._-]*'
    )
);

ALTER TABLE environment ADD COLUMN engine_host_name TEXT CHECK (
    engine_host_name IS NULL OR length(engine_host_name) BETWEEN 1 AND 255
);

-- How far each container log exporter rule has copied each Swarm service task's log.
CREATE TABLE service_log_cursor (
    rule_id            INTEGER NOT NULL REFERENCES automation_rule (id) ON DELETE CASCADE,
    -- Rule ids are reused after a delete, so a row is valid only while this matches the rule.
    rule_created_at    TEXT NOT NULL,
    environment_id     INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    service_id         TEXT NOT NULL CHECK (
                           length(service_id) = 25
                           AND service_id NOT GLOB '*[^0-9a-z]*'
                       ),
    -- Empty for lines whose attributes named no task; a WITHOUT ROWID key cannot hold null.
    task_id            TEXT NOT NULL CHECK (
                           task_id = ''
                           OR (length(task_id) = 25 AND task_id NOT GLOB '*[^0-9a-z]*')
                       ),
    last_timestamp_ns  INTEGER NOT NULL CHECK (last_timestamp_ns >= 0),
    lines_at_last      INTEGER NOT NULL CHECK (lines_at_last >= 0),
    object_path        TEXT CHECK (object_path IS NULL OR length(object_path) BETWEEN 1 AND 1024),
    finished           INTEGER NOT NULL DEFAULT 0 CHECK (finished IN (0, 1)),
    last_seen_at       TEXT NOT NULL CHECK (
                           length(last_seen_at) = 20
                           AND last_seen_at GLOB
                               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                       ),
    PRIMARY KEY (rule_id, environment_id, service_id, task_id)
) WITHOUT ROWID;

CREATE INDEX idx_service_log_cursor_environment_id ON service_log_cursor (environment_id);
