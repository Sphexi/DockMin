-- activity_event rebuilt with AUTOINCREMENT and the pruner among its actors, every row copied
-- with its id; an index for the action filter; and the export cursor of each exporter rule.

-- AUTOINCREMENT: an id removed by retention is never reissued below an export watermark.
CREATE TABLE activity_event_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    category         TEXT NOT NULL CHECK (
                         category IN ('authentication', 'user_action', 'automation')
                     ),
    actor_type       TEXT NOT NULL CHECK (actor_type IN ('user', 'subsystem', 'anonymous')),
    actor_user_id    INTEGER REFERENCES user (id) ON DELETE RESTRICT,
    actor_subsystem  TEXT CHECK (
                         actor_subsystem IS NULL
                         OR actor_subsystem IN (
                             'container-updater', 'container-health-checker',
                             'stack-restarter', 'backup-syncer',
                             'activity-log-exporter', 'environment-snapshot-refresher',
                             'activity-log-pruner'
                         )
                     ),
    rule_id          INTEGER REFERENCES automation_rule (id) ON DELETE SET NULL,
    job_run_id       INTEGER REFERENCES job_run (id) ON DELETE SET NULL,
    environment_id   INTEGER REFERENCES environment (id) ON DELETE SET NULL,
    target_type      TEXT NOT NULL,
    target_id        TEXT,
    action           TEXT NOT NULL,
    result           TEXT NOT NULL CHECK (result IN ('success', 'failure')),
    detail           TEXT,
    occurred_at      TEXT NOT NULL,
    CHECK (
        (actor_type = 'user' AND actor_user_id IS NOT NULL AND actor_subsystem IS NULL)
        OR (actor_type = 'subsystem' AND actor_subsystem IS NOT NULL AND actor_user_id IS NULL)
        OR (actor_type = 'anonymous' AND actor_user_id IS NULL AND actor_subsystem IS NULL)
    )
);

INSERT INTO activity_event_new (
    id, category, actor_type, actor_user_id, actor_subsystem, rule_id, job_run_id,
    environment_id, target_type, target_id, action, result, detail, occurred_at
)
SELECT
    id, category, actor_type, actor_user_id, actor_subsystem, rule_id, job_run_id,
    environment_id, target_type, target_id, action, result, detail, occurred_at
FROM activity_event
ORDER BY id;

-- No table references activity_event, so dropping it fires no foreign key action.
DROP TABLE activity_event;

ALTER TABLE activity_event_new RENAME TO activity_event;

CREATE INDEX idx_activity_event_occurred_at ON activity_event (occurred_at);

CREATE INDEX idx_activity_event_category ON activity_event (category, occurred_at);

CREATE INDEX idx_activity_event_target
    ON activity_event (environment_id, target_type, target_id, occurred_at);

CREATE INDEX idx_activity_event_actor_subsystem
    ON activity_event (actor_subsystem, occurred_at)
    WHERE actor_subsystem IS NOT NULL;

CREATE INDEX idx_activity_event_actor_user_id
    ON activity_event (actor_user_id, occurred_at)
    WHERE actor_user_id IS NOT NULL;

CREATE INDEX idx_activity_event_environment ON activity_event (environment_id, occurred_at);

CREATE INDEX idx_activity_event_rule_id
    ON activity_event (rule_id, occurred_at)
    WHERE rule_id IS NOT NULL;

CREATE INDEX idx_activity_event_job_run_id
    ON activity_event (job_run_id)
    WHERE job_run_id IS NOT NULL;

CREATE INDEX idx_activity_event_action ON activity_event (action, occurred_at);

-- The id of the last event an exporter rule has copied to its bucket; no row means nothing copied.
CREATE TABLE activity_export_cursor (
    rule_id        INTEGER PRIMARY KEY REFERENCES automation_rule (id) ON DELETE CASCADE,
    last_event_id  INTEGER NOT NULL CHECK (last_event_id >= 0),
    object_key     TEXT CHECK (object_key IS NULL OR length(object_key) BETWEEN 1 AND 1024),
    exported_at    TEXT
);
