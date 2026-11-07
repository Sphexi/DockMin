-- automation_rule, scheduled_job and activity_event rebuilt to admit container-log-exporter and
-- notification-sender, every row copied with its id and every index recreated.

CREATE TABLE automation_rule_new (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    subsystem           TEXT NOT NULL CHECK (
                            subsystem IN (
                                'container-updater', 'container-health-checker',
                                'stack-restarter', 'backup-syncer', 'activity-log-exporter',
                                'container-log-exporter'
                            )
                        ),
    environment_id      INTEGER REFERENCES environment (id) ON DELETE CASCADE,
    target_selector     TEXT NOT NULL,
    -- Unencrypted: must never hold a secret.
    parameters          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(parameters)),
    is_enabled          INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    paused_until        TEXT,
    created_by_user_id  INTEGER REFERENCES user (id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

INSERT INTO automation_rule_new (
    id, name, subsystem, environment_id, target_selector, parameters, is_enabled, paused_until,
    created_by_user_id, created_at, updated_at
)
SELECT
    id, name, subsystem, environment_id, target_selector, parameters, is_enabled, paused_until,
    created_by_user_id, created_at, updated_at
FROM automation_rule
ORDER BY id;

DROP TABLE automation_rule;

ALTER TABLE automation_rule_new RENAME TO automation_rule;

CREATE INDEX idx_automation_rule_environment_id ON automation_rule (environment_id);

CREATE UNIQUE INDEX idx_automation_rule_owner_name
    ON automation_rule (subsystem, created_by_user_id, lower(name))
    WHERE created_by_user_id IS NOT NULL;

CREATE INDEX idx_automation_rule_owner ON automation_rule (created_by_user_id)
    WHERE created_by_user_id IS NOT NULL;

CREATE TABLE scheduled_job_new (
    id                    INTEGER PRIMARY KEY,
    subsystem             TEXT NOT NULL CHECK (
                              subsystem IN (
                                  'container-updater', 'container-health-checker',
                                  'stack-restarter', 'backup-syncer',
                                  'activity-log-exporter', 'environment-snapshot-refresher',
                                  'container-log-exporter', 'notification-sender'
                              )
                          ),
    rule_id               INTEGER REFERENCES automation_rule (id) ON DELETE CASCADE,
    schedule_kind         TEXT NOT NULL CHECK (schedule_kind IN ('cron', 'interval')),
    schedule_expression   TEXT NOT NULL,
    next_run_at           TEXT,
    overrun_policy        TEXT NOT NULL DEFAULT 'skip'
                          CHECK (overrun_policy IN ('skip', 'queue', 'cancel_previous')),
    catch_up              INTEGER NOT NULL DEFAULT 0 CHECK (catch_up IN (0, 1)),
    is_enabled            INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    paused_until          TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    time_zone             TEXT NOT NULL DEFAULT 'UTC',
    failure_threshold     INTEGER
                          CHECK (failure_threshold IS NULL OR failure_threshold BETWEEN 1 AND 100),
    environment_id        INTEGER REFERENCES environment (id) ON DELETE CASCADE,
    last_run_at           TEXT,
    last_run_outcome      TEXT CHECK (last_run_outcome IS NULL OR last_run_outcome = 'success')
);

INSERT INTO scheduled_job_new (
    id, subsystem, rule_id, schedule_kind, schedule_expression, next_run_at, overrun_policy,
    catch_up, is_enabled, paused_until, consecutive_failures, created_at, updated_at, time_zone,
    failure_threshold, environment_id, last_run_at, last_run_outcome
)
SELECT
    id, subsystem, rule_id, schedule_kind, schedule_expression, next_run_at, overrun_policy,
    catch_up, is_enabled, paused_until, consecutive_failures, created_at, updated_at, time_zone,
    failure_threshold, environment_id, last_run_at, last_run_outcome
FROM scheduled_job
ORDER BY id;

DROP TABLE scheduled_job;

ALTER TABLE scheduled_job_new RENAME TO scheduled_job;

CREATE INDEX idx_scheduled_job_next_run_at ON scheduled_job (next_run_at) WHERE is_enabled = 1;

CREATE INDEX idx_scheduled_job_environment_id ON scheduled_job (environment_id)
    WHERE environment_id IS NOT NULL;

CREATE UNIQUE INDEX idx_scheduled_job_rule_id ON scheduled_job (rule_id) WHERE rule_id IS NOT NULL;

CREATE UNIQUE INDEX idx_scheduled_job_infrastructure ON scheduled_job (subsystem)
    WHERE rule_id IS NULL;

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
                             'activity-log-pruner', 'container-log-exporter',
                             'notification-sender'
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

-- The copy leaves the sequence at the highest surviving id; a pruned id must never be reissued.
DELETE FROM sqlite_sequence WHERE name = 'activity_event_new';

INSERT INTO sqlite_sequence (name, seq)
SELECT 'activity_event_new', max(seq, (SELECT coalesce(max(id), 0) FROM activity_event_new))
FROM sqlite_sequence
WHERE name = 'activity_event';

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
