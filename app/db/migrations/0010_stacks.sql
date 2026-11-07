-- Compose stacks: the stack table rebuilt with its file, digests and last operation, and the
-- variables a stack deploys with.

-- Rebuild: SQLite cannot add these CHECKs in place. The guard aborts if the rebuild would discard a row.
CREATE TABLE migration_guard (
    stack_table_is_empty INTEGER NOT NULL CHECK (stack_table_is_empty = 1)
);

INSERT INTO migration_guard (stack_table_is_empty)
SELECT CASE WHEN EXISTS (SELECT 1 FROM stack) THEN 0 ELSE 1 END;

DROP TABLE migration_guard;

-- AUTOINCREMENT: a deleted stack's id is never reissued.
CREATE TABLE stack_new (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id        INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    name                  TEXT NOT NULL,
    kind                  TEXT NOT NULL CHECK (kind IN ('compose', 'swarm')),
    origin                TEXT NOT NULL CHECK (origin IN ('created', 'adopted')),
    project_directory     TEXT NOT NULL UNIQUE CHECK (
                              length(project_directory) = 32
                              AND project_directory NOT GLOB '*[^0-9a-f]*'
                          ),
    compose_file          TEXT NOT NULL CHECK (
                              length(CAST(compose_file AS BLOB)) BETWEEN 1 AND 131072
                          ),
    compose_file_digest   TEXT NOT NULL CHECK (
                              length(compose_file_digest) = 64
                              AND compose_file_digest NOT GLOB '*[^0-9a-f]*'
                          ),
    deployed_file_digest  TEXT CHECK (
                              deployed_file_digest IS NULL
                              OR (
                                  length(deployed_file_digest) = 64
                                  AND deployed_file_digest NOT GLOB '*[^0-9a-f]*'
                              )
                          ),
    resolved_digest       TEXT CHECK (
                              resolved_digest IS NULL
                              OR (
                                  length(resolved_digest) = 64
                                  AND resolved_digest NOT GLOB '*[^0-9a-f]*'
                              )
                          ),
    status                TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (status IN ('active', 'partial', 'inactive', 'error', 'unknown')),
    last_operation        TEXT CHECK (
                              last_operation IS NULL
                              OR last_operation IN (
                                  'deploy', 'redeploy', 'start', 'stop', 'adopt', 'remove'
                              )
                          ),
    last_outcome          TEXT CHECK (
                              last_outcome IS NULL
                              OR last_outcome IN
                                  ('success', 'failure', 'interrupted', 'outcome_unknown')
                          ),
    last_operation_at     TEXT,
    last_deployed_at      TEXT,
    variables_changed_at  TEXT,
    created_by_user_id    INTEGER REFERENCES user (id) ON DELETE SET NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (environment_id, name),
    -- A compose name is held to the character set Compose accepts; a swarm name follows the Swarm rules.
    CHECK (
        kind <> 'compose' OR (
            length(name) BETWEEN 1 AND 63
            AND name NOT GLOB '*[^a-z0-9_-]*'
            AND substr(name, 1, 1) GLOB '[a-z0-9]'
        )
    ),
    CHECK ((last_operation IS NULL) = (last_outcome IS NULL))
);

DROP TABLE stack;

ALTER TABLE stack_new RENAME TO stack;

CREATE INDEX idx_stack_created_by_user_id
    ON stack (created_by_user_id)
    WHERE created_by_user_id IS NOT NULL;

CREATE TABLE stack_variable (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    stack_id          INTEGER NOT NULL REFERENCES stack (id) ON DELETE CASCADE,
    name              TEXT NOT NULL CHECK (
                          length(name) BETWEEN 1 AND 128
                          AND name NOT GLOB '*[^A-Za-z0-9_]*'
                          AND substr(name, 1, 1) GLOB '[A-Za-z_]'
                      ),
    is_secret         INTEGER NOT NULL CHECK (is_secret IN (0, 1)),
    value             TEXT CHECK (value IS NULL OR length(CAST(value AS BLOB)) <= 8192),
    value_ciphertext  TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (stack_id, name),
    CHECK (
        (is_secret = 0 AND value IS NOT NULL AND value_ciphertext IS NULL)
        OR (is_secret = 1 AND value IS NULL AND value_ciphertext IS NOT NULL)
    )
);
