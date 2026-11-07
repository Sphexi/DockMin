-- Run history gains a trigger, a partial outcome, machine-readable reasons and the requesting
-- user; scheduled jobs gain a time zone, a failure threshold, their environment and one-job
-- uniqueness; rule names become unique per owner.

-- The guard aborts unless job_run and automation_rule are empty: dropping a parent with rows fires its children's foreign key actions.
CREATE TABLE migration_guard (
    job_run_is_empty              INTEGER NOT NULL CHECK (job_run_is_empty = 1),
    automation_rule_is_empty      INTEGER NOT NULL CHECK (automation_rule_is_empty = 1),
    one_job_per_rule              INTEGER NOT NULL CHECK (one_job_per_rule = 1),
    one_infrastructure_job_each   INTEGER NOT NULL CHECK (one_infrastructure_job_each = 1)
);

INSERT INTO migration_guard (
    job_run_is_empty, automation_rule_is_empty, one_job_per_rule, one_infrastructure_job_each
)
SELECT
    CASE WHEN EXISTS (SELECT 1 FROM job_run) THEN 0 ELSE 1 END,
    CASE WHEN EXISTS (SELECT 1 FROM automation_rule) THEN 0 ELSE 1 END,
    CASE WHEN EXISTS (
        SELECT 1 FROM scheduled_job
        WHERE rule_id IS NOT NULL
        GROUP BY rule_id
        HAVING COUNT(*) > 1
    ) THEN 0 ELSE 1 END,
    CASE WHEN EXISTS (
        SELECT 1 FROM scheduled_job
        WHERE rule_id IS NULL
        GROUP BY subsystem
        HAVING COUNT(*) > 1
    ) THEN 0 ELSE 1 END;

DROP TABLE migration_guard;

CREATE TABLE job_run_new (
    -- AUTOINCREMENT: a pruned run's id is never reissued to a different run.
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_job_id    INTEGER NOT NULL REFERENCES scheduled_job (id) ON DELETE CASCADE,
    trigger             TEXT NOT NULL
                        CHECK (trigger IN ('schedule', 'catch_up', 'queued', 'manual', 'dry_run')),
    requested_by_user_id INTEGER REFERENCES user (id) ON DELETE SET NULL,
    scheduled_for       TEXT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    heartbeat_at        TEXT,
    outcome             TEXT CHECK (
                            outcome IS NULL
                            OR outcome IN ('success', 'partial', 'failure', 'skipped', 'interrupted')
                        ),
    reason              TEXT CHECK (
                            reason IS NULL
                            OR (length(reason) BETWEEN 1 AND 64 AND reason NOT GLOB '*[^a-z_]*')
                        ),
    message             TEXT CHECK (message IS NULL OR length(message) <= 512),
    detail              TEXT CHECK (detail IS NULL OR json_valid(detail)),
    CHECK ((outcome IS NULL) = (finished_at IS NULL)),
    CHECK (outcome IS NULL OR outcome IN ('success', 'partial') OR reason IS NOT NULL)
);

DROP TABLE job_run;

ALTER TABLE job_run_new RENAME TO job_run;

CREATE INDEX idx_job_run_job ON job_run (scheduled_job_id, id);

CREATE INDEX idx_job_run_in_flight ON job_run (heartbeat_at) WHERE outcome IS NULL;

CREATE INDEX idx_job_run_requested_by ON job_run (requested_by_user_id)
    WHERE requested_by_user_id IS NOT NULL;

CREATE TABLE automation_rule_new (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    subsystem           TEXT NOT NULL CHECK (
                            subsystem IN (
                                'container-updater', 'container-health-checker',
                                'stack-restarter', 'backup-syncer', 'activity-log-exporter'
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

DROP TABLE automation_rule;

ALTER TABLE automation_rule_new RENAME TO automation_rule;

CREATE INDEX idx_automation_rule_environment_id ON automation_rule (environment_id);

-- lower() folds ASCII only.
CREATE UNIQUE INDEX idx_automation_rule_owner_name
    ON automation_rule (subsystem, created_by_user_id, lower(name))
    WHERE created_by_user_id IS NOT NULL;

CREATE INDEX idx_automation_rule_owner ON automation_rule (created_by_user_id)
    WHERE created_by_user_id IS NOT NULL;

ALTER TABLE scheduled_job ADD COLUMN time_zone TEXT NOT NULL DEFAULT 'UTC';

ALTER TABLE scheduled_job ADD COLUMN failure_threshold INTEGER
    CHECK (failure_threshold IS NULL OR failure_threshold BETWEEN 1 AND 100);

ALTER TABLE scheduled_job ADD COLUMN environment_id INTEGER
    REFERENCES environment (id) ON DELETE CASCADE;

CREATE INDEX idx_scheduled_job_environment_id ON scheduled_job (environment_id)
    WHERE environment_id IS NOT NULL;

DROP INDEX idx_scheduled_job_rule_id;

CREATE UNIQUE INDEX idx_scheduled_job_rule_id ON scheduled_job (rule_id) WHERE rule_id IS NOT NULL;

CREATE UNIQUE INDEX idx_scheduled_job_infrastructure ON scheduled_job (subsystem)
    WHERE rule_id IS NULL;
