-- Registry credential sets: ids never reissued, the remaining shape constraints, an address
-- index, and grants to users and teams.

-- Rebuild: SQLite cannot add a CHECK in place. The guard aborts if registry_credential or a referencing table holds rows.
CREATE TABLE migration_guard (
    registry_tables_are_empty INTEGER NOT NULL CHECK (registry_tables_are_empty = 1)
);

INSERT INTO migration_guard (registry_tables_are_empty)
SELECT CASE
    WHEN EXISTS (SELECT 1 FROM registry_credential)
        OR EXISTS (SELECT 1 FROM registry_environment_access)
        OR EXISTS (SELECT 1 FROM image_credential_binding) THEN 0
    ELSE 1
END;

DROP TABLE migration_guard;

-- AUTOINCREMENT: a deleted set's id is never reissued.
CREATE TABLE registry_credential_new (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    name                         TEXT NOT NULL UNIQUE COLLATE NOCASE
                                 CHECK (length(name) BETWEEN 1 AND 64),
    provider                     TEXT NOT NULL CHECK (
                                     provider IN (
                                         'dockerhub', 'ghcr', 'acr',
                                         'gitlab', 'ecr', 'custom'
                                     )
                                 ),
    url                          TEXT NOT NULL CHECK (length(url) BETWEEN 1 AND 262),
    authentication_enabled       INTEGER NOT NULL
                                 CHECK (authentication_enabled IN (0, 1)),
    username                     TEXT
                                 CHECK (username IS NULL OR length(username) BETWEEN 1 AND 256),
    secret_kind                  TEXT NOT NULL CHECK (secret_kind IN ('static', 'derived')),
    -- No format CHECK: a row is inserted with an empty placeholder and sealed by a later
    -- statement in the same transaction.
    secret_ciphertext            TEXT,
    derived_secret_ciphertext    TEXT,
    derived_secret_expires_at    TEXT,
    derived_secret_refreshed_at  TEXT,
    -- Unencrypted: must never hold a secret.
    provider_extras              TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(provider_extras)),
    verify_tls                   INTEGER NOT NULL DEFAULT 1 CHECK (verify_tls IN (0, 1)),
    ca_bundle                    TEXT
                                 CHECK (ca_bundle IS NULL OR length(ca_bundle) BETWEEN 1 AND 65536),
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
    ),
    CHECK (authentication_enabled = 0 OR username IS NOT NULL),
    CHECK (secret_kind = 'static' OR authentication_enabled = 1),
    CHECK ((provider = 'ecr') = (secret_kind = 'derived')),
    CHECK (verify_tls = 1 OR authentication_enabled = 0),
    CHECK (ca_bundle IS NULL OR verify_tls = 1),
    -- json_type raises on malformed text, so validity is tested first.
    CHECK (
        CASE WHEN json_valid(provider_extras) THEN json_type(provider_extras) = 'object' ELSE 0 END
    )
);

DROP TABLE registry_credential;

ALTER TABLE registry_credential_new RENAME TO registry_credential;

CREATE INDEX idx_registry_credential_url ON registry_credential (url, name);

CREATE TABLE registry_credential_user_access (
    id                      INTEGER PRIMARY KEY,
    registry_credential_id  INTEGER NOT NULL
                            REFERENCES registry_credential (id) ON DELETE CASCADE,
    user_id                 INTEGER NOT NULL REFERENCES user (id) ON DELETE CASCADE,
    UNIQUE (registry_credential_id, user_id)
);

CREATE INDEX idx_registry_credential_user_access_user_id
    ON registry_credential_user_access (user_id, registry_credential_id);

CREATE TABLE registry_credential_team_access (
    id                      INTEGER PRIMARY KEY,
    registry_credential_id  INTEGER NOT NULL
                            REFERENCES registry_credential (id) ON DELETE CASCADE,
    team_id                 INTEGER NOT NULL REFERENCES team (id) ON DELETE CASCADE,
    UNIQUE (registry_credential_id, team_id)
);

CREATE INDEX idx_registry_credential_team_access_team_id
    ON registry_credential_team_access (team_id, registry_credential_id);
