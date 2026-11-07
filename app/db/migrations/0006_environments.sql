-- Environment status and Swarm facts, one environment per endpoint, and grants on environments
-- and environment groups.

-- Rebuild: SQLite cannot change a CHECK in place. The guard aborts if environment holds rows, whose drop would cascade.
CREATE TABLE migration_guard (
    environment_is_empty INTEGER NOT NULL CHECK (environment_is_empty = 1)
);

INSERT INTO migration_guard (environment_is_empty)
SELECT CASE WHEN EXISTS (SELECT 1 FROM environment) THEN 0 ELSE 1 END;

DROP TABLE migration_guard;

-- AUTOINCREMENT: a deleted environment's id is never reissued.
CREATE TABLE environment_new (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    name                     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type                     TEXT CHECK (type IS NULL OR type IN ('standalone', 'swarm')),
    connection_kind          TEXT NOT NULL CHECK (connection_kind IN ('unix_socket', 'tcp_tls')),
    socket_path              TEXT,
    url                      TEXT,
    environment_group_id     INTEGER REFERENCES environment_group (id) ON DELETE SET NULL,
    status                   TEXT NOT NULL DEFAULT 'unknown' CHECK (
                                 status IN (
                                     'unknown', 'reachable', 'unreachable', 'version_unsupported'
                                 )
                             ),
    failed_stage             TEXT CHECK (
                                 failed_stage IS NULL
                                 OR failed_stage IN (
                                     'dns', 'tcp', 'socket', 'tls_handshake', 'certificate',
                                     'engine'
                                 )
                             ),
    status_message           TEXT,
    last_checked_at          TEXT,
    last_contacted_at        TEXT,
    engine_version           TEXT,
    api_version              TEXT,
    swarm_state              TEXT CHECK (
                                 swarm_state IS NULL
                                 OR swarm_state IN (
                                     'inactive', 'pending', 'active', 'error', 'locked'
                                 )
                             ),
    swarm_control_available  INTEGER CHECK (
                                 swarm_control_available IS NULL
                                 OR swarm_control_available IN (0, 1)
                             ),
    compose_cli_version      TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    CHECK (
        (connection_kind = 'unix_socket' AND socket_path IS NOT NULL AND url IS NULL)
        OR (connection_kind = 'tcp_tls' AND url IS NOT NULL AND socket_path IS NULL)
    ),
    CHECK (failed_stage IS NULL OR status IN ('unreachable', 'version_unsupported'))
);

DROP TABLE environment;

ALTER TABLE environment_new RENAME TO environment;

-- Partial: a duplicate lookup must state the connection kind for the index to serve it.
CREATE UNIQUE INDEX idx_environment_socket_path
    ON environment (socket_path)
    WHERE connection_kind = 'unix_socket';

CREATE UNIQUE INDEX idx_environment_url
    ON environment (url)
    WHERE connection_kind = 'tcp_tls';

CREATE INDEX idx_environment_group_id ON environment (environment_group_id, name);

CREATE INDEX idx_environment_status ON environment (status, name);

CREATE TABLE environment_user_access (
    id              INTEGER PRIMARY KEY,
    environment_id  INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES user (id) ON DELETE CASCADE,
    UNIQUE (environment_id, user_id)
);

CREATE INDEX idx_environment_user_access_user_id
    ON environment_user_access (user_id, environment_id);

CREATE TABLE environment_team_access (
    id              INTEGER PRIMARY KEY,
    environment_id  INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL REFERENCES team (id) ON DELETE CASCADE,
    UNIQUE (environment_id, team_id)
);

CREATE INDEX idx_environment_team_access_team_id
    ON environment_team_access (team_id, environment_id);

CREATE TABLE environment_group_user_access (
    id                    INTEGER PRIMARY KEY,
    environment_group_id  INTEGER NOT NULL REFERENCES environment_group (id) ON DELETE CASCADE,
    user_id               INTEGER NOT NULL REFERENCES user (id) ON DELETE CASCADE,
    UNIQUE (environment_group_id, user_id)
);

CREATE INDEX idx_environment_group_user_access_user_id
    ON environment_group_user_access (user_id, environment_group_id);

CREATE TABLE environment_group_team_access (
    id                    INTEGER PRIMARY KEY,
    environment_group_id  INTEGER NOT NULL REFERENCES environment_group (id) ON DELETE CASCADE,
    team_id               INTEGER NOT NULL REFERENCES team (id) ON DELETE CASCADE,
    UNIQUE (environment_group_id, team_id)
);

CREATE INDEX idx_environment_group_team_access_team_id
    ON environment_group_team_access (team_id, environment_group_id);
