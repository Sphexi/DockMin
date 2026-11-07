-- Rejects malformed JSON in the two free-form columns that stay unencrypted.
-- SQLite cannot add a CHECK to an existing column, so each table is rebuilt.
-- The guard aborts unless both tables are empty: dropping a table with rows fires its children's foreign key actions.

CREATE TABLE migration_guard (
    tables_are_empty INTEGER NOT NULL CHECK (tables_are_empty = 1)
);

INSERT INTO migration_guard (tables_are_empty)
SELECT CASE
    WHEN EXISTS (SELECT 1 FROM registry_credential)
        OR EXISTS (SELECT 1 FROM automation_rule) THEN 0
    ELSE 1
END;

DROP TABLE migration_guard;

CREATE TABLE registry_credential_new (
    id                           INTEGER PRIMARY KEY,
    name                         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    provider                     TEXT NOT NULL CHECK (
                                     provider IN (
                                         'dockerhub', 'ghcr', 'acr',
                                         'gitlab', 'ecr', 'custom'
                                     )
                                 ),
    url                          TEXT NOT NULL,
    authentication_enabled       INTEGER NOT NULL
                                 CHECK (authentication_enabled IN (0, 1)),
    username                     TEXT,
    secret_kind                  TEXT NOT NULL CHECK (secret_kind IN ('static', 'derived')),
    secret_ciphertext            TEXT,
    derived_secret_ciphertext    TEXT,
    derived_secret_expires_at    TEXT,
    derived_secret_refreshed_at  TEXT,
    -- Unencrypted: must never hold a secret.
    provider_extras              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(provider_extras)),
    verify_tls                   INTEGER NOT NULL DEFAULT 1 CHECK (verify_tls IN (0, 1)),
    ca_bundle                    TEXT,
    is_default_for_host          INTEGER NOT NULL DEFAULT 0
                                 CHECK (is_default_for_host IN (0, 1)),
    created_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL,
    CHECK (
        (authentication_enabled = 1 AND secret_ciphertext IS NOT NULL)
        OR (
            authentication_enabled = 0
            AND username IS NULL
            AND secret_ciphertext IS NULL
            AND derived_secret_ciphertext IS NULL
        )
    ),
    CHECK (
        secret_kind = 'derived'
        OR (
            derived_secret_ciphertext IS NULL
            AND derived_secret_expires_at IS NULL
            AND derived_secret_refreshed_at IS NULL
        )
    ),
    CHECK (
        (derived_secret_ciphertext IS NULL AND derived_secret_expires_at IS NULL)
        OR (derived_secret_ciphertext IS NOT NULL AND derived_secret_expires_at IS NOT NULL)
    )
);

DROP TABLE registry_credential;

ALTER TABLE registry_credential_new RENAME TO registry_credential;

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
    updated_at          TEXT NOT NULL,
    UNIQUE (subsystem, name)
);

DROP TABLE automation_rule;

ALTER TABLE automation_rule_new RENAME TO automation_rule;

CREATE INDEX idx_automation_rule_environment_id ON automation_rule (environment_id);
