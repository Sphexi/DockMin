-- When a container held by a health-checker rule was last seen unhealthy by that rule.

CREATE TABLE restart_hold_sighting (
    rule_id            INTEGER NOT NULL REFERENCES automation_rule (id) ON DELETE CASCADE,
    environment_id     INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    container_id       TEXT NOT NULL CHECK (
                           length(container_id) = 64
                           AND container_id NOT GLOB '*[^0-9a-f]*'
                       ),
    -- Compared as text, so only the stored form fits.
    last_unhealthy_at  TEXT NOT NULL CHECK (
                           length(last_unhealthy_at) = 20
                           AND last_unhealthy_at GLOB
                               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                       ),
    PRIMARY KEY (rule_id, environment_id, container_id)
) WITHOUT ROWID;

CREATE INDEX idx_restart_hold_sighting_environment_id ON restart_hold_sighting (environment_id);
