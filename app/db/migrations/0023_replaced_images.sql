-- Images an update replaced, waiting for the grace period of the rule that replaced them.

CREATE TABLE replaced_image (
    rule_id          INTEGER NOT NULL REFERENCES automation_rule (id) ON DELETE CASCADE,
    rule_created_at  TEXT NOT NULL,
    environment_id   INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    image_id         TEXT NOT NULL CHECK (
                         length(image_id) = 64
                         AND image_id NOT GLOB '*[^0-9a-f]*'
                     ),
    -- Compared as text, so only the stored form fits.
    replaced_at      TEXT NOT NULL CHECK (
                         length(replaced_at) = 20
                         AND replaced_at GLOB
                             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                     ),
    target_type      TEXT NOT NULL CHECK (target_type IN ('container', 'stack')),
    target_id        TEXT NOT NULL CHECK (length(target_id) BETWEEN 1 AND 256),
    PRIMARY KEY (rule_id, environment_id, image_id)
) WITHOUT ROWID;

CREATE INDEX idx_replaced_image_environment_id ON replaced_image (environment_id);
