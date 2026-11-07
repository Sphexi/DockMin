-- stack_dismissal: unmanaged stack entries an administrator has dismissed, with the objects they held.

CREATE TABLE stack_dismissal (
    environment_id        INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    kind                  TEXT NOT NULL CHECK (kind IN ('compose', 'swarm')),
    project_key           TEXT NOT NULL CHECK (length(project_key) BETWEEN 1 AND 63),
    -- json_type raises on malformed text, so validity is tested first.
    objects               TEXT NOT NULL CHECK (
                              CASE WHEN json_valid(objects)
                                   THEN json_type(objects) = 'array'
                                        AND json_array_length(objects) BETWEEN 1 AND 500
                                   ELSE 0 END
                          ),
    dismissed_by_user_id  INTEGER REFERENCES user (id) ON DELETE SET NULL,
    dismissed_at          TEXT NOT NULL,
    PRIMARY KEY (environment_id, kind, project_key)
) WITHOUT ROWID;
