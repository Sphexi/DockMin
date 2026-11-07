-- Initial schema. Timestamps are ISO 8601 UTC text with a Z suffix, so they sort lexicographically.

CREATE TABLE setting (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    value_type  TEXT NOT NULL CHECK (value_type IN ('string', 'integer', 'boolean', 'json')),
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE user (
    id             INTEGER PRIMARY KEY,
    -- NOCASE folds ASCII only; a username is restricted to ASCII.
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN ('administrator', 'standard')),
    is_enabled     INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    created_at     TEXT NOT NULL,
    last_login_at  TEXT
);

CREATE TABLE team (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at  TEXT NOT NULL
);

CREATE TABLE team_membership (
    id          INTEGER PRIMARY KEY,
    team_id     INTEGER NOT NULL REFERENCES team (id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES user (id) ON DELETE CASCADE,
    is_leader   INTEGER NOT NULL DEFAULT 0 CHECK (is_leader IN (0, 1)),
    created_at  TEXT NOT NULL,
    UNIQUE (team_id, user_id)
);

CREATE INDEX idx_team_membership_user_id ON team_membership (user_id);

CREATE TABLE api_token (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES user (id) ON DELETE CASCADE,
    label         TEXT NOT NULL,
    prefix        TEXT NOT NULL UNIQUE,
    token_hash    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    expires_at    TEXT,
    revoked_at    TEXT,
    UNIQUE (user_id, label)
);

CREATE INDEX idx_api_token_user_id ON api_token (user_id);

CREATE TABLE session (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES user (id) ON DELETE CASCADE,
    token_hash       TEXT NOT NULL UNIQUE,
    issued_at        TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    revoked_at       TEXT,
    user_agent       TEXT,
    remote_address   TEXT
);

CREATE INDEX idx_session_user_id ON session (user_id);
CREATE INDEX idx_session_expires_at ON session (expires_at);

CREATE TABLE environment_group (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE environment (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type                  TEXT NOT NULL CHECK (type IN ('standalone', 'swarm')),
    connection_kind       TEXT NOT NULL CHECK (connection_kind IN ('unix_socket', 'tcp_tls')),
    socket_path           TEXT,
    url                   TEXT,
    environment_group_id  INTEGER REFERENCES environment_group (id) ON DELETE SET NULL,
    status                TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (status IN ('up', 'down', 'unknown')),
    last_contacted_at     TEXT,
    engine_version        TEXT,
    api_version           TEXT,
    compose_cli_version   TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    CHECK (
        (connection_kind = 'unix_socket' AND socket_path IS NOT NULL AND url IS NULL)
        OR (connection_kind = 'tcp_tls' AND url IS NOT NULL AND socket_path IS NULL)
    )
);

CREATE INDEX idx_environment_group_id ON environment (environment_group_id);

CREATE TABLE environment_tls_material (
    id                     INTEGER PRIMARY KEY,
    environment_id         INTEGER NOT NULL UNIQUE
                           REFERENCES environment (id) ON DELETE CASCADE,
    ca_certificate         TEXT,
    client_certificate     TEXT,
    client_key_ciphertext  TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE tag (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at  TEXT NOT NULL
);

CREATE TABLE environment_tag (
    id              INTEGER PRIMARY KEY,
    environment_id  INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    tag_id          INTEGER NOT NULL REFERENCES tag (id) ON DELETE CASCADE,
    UNIQUE (environment_id, tag_id)
);

CREATE INDEX idx_environment_tag_tag_id ON environment_tag (tag_id);

CREATE TABLE environment_snapshot (
    id                       INTEGER PRIMARY KEY,
    environment_id           INTEGER NOT NULL UNIQUE
                             REFERENCES environment (id) ON DELETE CASCADE,
    captured_at              TEXT NOT NULL,
    container_count          INTEGER,
    container_running_count  INTEGER,
    container_stopped_count  INTEGER,
    image_count              INTEGER,
    volume_count             INTEGER,
    network_count            INTEGER,
    stack_count              INTEGER,
    service_count            INTEGER,
    swarm_node_count         INTEGER,
    error                    TEXT
);

CREATE TABLE resource_control (
    id                       INTEGER PRIMARY KEY,
    environment_id           INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    resource_type            TEXT NOT NULL CHECK (
                                 resource_type IN (
                                     'container', 'service', 'volume',
                                     'secret', 'config', 'stack', 'network'
                                 )
                             ),
    resource_id              TEXT NOT NULL,
    is_public                INTEGER NOT NULL DEFAULT 0 CHECK (is_public IN (0, 1)),
    is_administrators_only   INTEGER NOT NULL DEFAULT 0
                             CHECK (is_administrators_only IN (0, 1)),
    access_level             TEXT NOT NULL CHECK (access_level IN ('read', 'write')),
    owner_user_id            INTEGER REFERENCES user (id) ON DELETE SET NULL,
    -- Denormalised cache for display; never the authority on visibility.
    sub_resource_ids         TEXT NOT NULL DEFAULT '[]',
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    UNIQUE (environment_id, resource_type, resource_id),
    CHECK (NOT (is_public = 1 AND is_administrators_only = 1))
);

CREATE TABLE resource_control_user_access (
    id                   INTEGER PRIMARY KEY,
    resource_control_id  INTEGER NOT NULL REFERENCES resource_control (id) ON DELETE CASCADE,
    user_id              INTEGER NOT NULL REFERENCES user (id) ON DELETE CASCADE,
    UNIQUE (resource_control_id, user_id)
);

CREATE INDEX idx_resource_control_user_access_user_id ON resource_control_user_access (user_id);

CREATE TABLE resource_control_team_access (
    id                   INTEGER PRIMARY KEY,
    resource_control_id  INTEGER NOT NULL REFERENCES resource_control (id) ON DELETE CASCADE,
    team_id              INTEGER NOT NULL REFERENCES team (id) ON DELETE CASCADE,
    UNIQUE (resource_control_id, team_id)
);

CREATE INDEX idx_resource_control_team_access_team_id ON resource_control_team_access (team_id);

CREATE TABLE registry_credential (
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
    provider_extras              TEXT NOT NULL DEFAULT '{}',
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

CREATE TABLE registry_environment_access (
    id                      INTEGER PRIMARY KEY,
    registry_credential_id  INTEGER NOT NULL
                            REFERENCES registry_credential (id) ON DELETE CASCADE,
    environment_id          INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    UNIQUE (registry_credential_id, environment_id)
);

CREATE INDEX idx_registry_environment_access_environment_id
    ON registry_environment_access (environment_id);

CREATE TABLE image_credential_binding (
    id                      INTEGER PRIMARY KEY,
    environment_id          INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    image_reference         TEXT NOT NULL,
    -- RESTRICT: delete a credential set only after moving its bindings to 'credential_missing'.
    registry_credential_id  INTEGER REFERENCES registry_credential (id) ON DELETE RESTRICT,
    observed_digest         TEXT,
    state                   TEXT NOT NULL CHECK (state IN ('bound', 'credential_missing')),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE (environment_id, image_reference),
    CHECK (state = 'bound' OR registry_credential_id IS NULL)
);

CREATE INDEX idx_image_credential_binding_registry_credential_id
    ON image_credential_binding (registry_credential_id);

CREATE TABLE stack (
    id                   INTEGER PRIMARY KEY,
    environment_id       INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,
    kind                 TEXT NOT NULL CHECK (kind IN ('compose', 'swarm')),
    project_directory    TEXT NOT NULL,
    compose_file_digest  TEXT,
    status               TEXT NOT NULL DEFAULT 'unknown'
                         CHECK (status IN ('active', 'inactive', 'error', 'unknown')),
    created_by_user_id   INTEGER REFERENCES user (id) ON DELETE SET NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (environment_id, name)
);

CREATE TABLE automation_rule (
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
    -- Validated by the owning subsystem's model on read and write, not here.
    parameters          TEXT NOT NULL DEFAULT '{}',
    is_enabled          INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    paused_until        TEXT,
    created_by_user_id  INTEGER REFERENCES user (id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (subsystem, name)
);

CREATE INDEX idx_automation_rule_environment_id ON automation_rule (environment_id);

CREATE TABLE scheduled_job (
    id                    INTEGER PRIMARY KEY,
    subsystem             TEXT NOT NULL CHECK (
                              subsystem IN (
                                  'container-updater', 'container-health-checker',
                                  'stack-restarter', 'backup-syncer',
                                  'activity-log-exporter', 'environment-snapshot-refresher'
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
    updated_at            TEXT NOT NULL
);

CREATE INDEX idx_scheduled_job_next_run_at ON scheduled_job (next_run_at) WHERE is_enabled = 1;

CREATE INDEX idx_scheduled_job_rule_id ON scheduled_job (rule_id);

CREATE TABLE job_run (
    id                INTEGER PRIMARY KEY,
    scheduled_job_id  INTEGER NOT NULL REFERENCES scheduled_job (id) ON DELETE CASCADE,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    outcome           TEXT CHECK (
                          outcome IS NULL
                          OR outcome IN ('success', 'failure', 'skipped', 'interrupted')
                      ),
    error             TEXT,
    heartbeat_at      TEXT,
    CHECK ((outcome IS NULL) = (finished_at IS NULL))
);

CREATE INDEX idx_job_run_scheduled_job_id ON job_run (scheduled_job_id, started_at DESC);

CREATE INDEX idx_job_run_in_flight ON job_run (heartbeat_at) WHERE outcome IS NULL;

CREATE TABLE activity_event (
    id               INTEGER PRIMARY KEY,
    category         TEXT NOT NULL CHECK (
                         category IN ('authentication', 'user_action', 'automation')
                     ),
    actor_type       TEXT NOT NULL CHECK (actor_type IN ('user', 'subsystem', 'anonymous')),
    -- RESTRICT: an account is retired with is_enabled, never deleted while it has events.
    actor_user_id    INTEGER REFERENCES user (id) ON DELETE RESTRICT,
    -- Deliberately not a foreign key: a subsystem is a class, not a row.
    actor_subsystem  TEXT CHECK (
                         actor_subsystem IS NULL
                         OR actor_subsystem IN (
                             'container-updater', 'container-health-checker',
                             'stack-restarter', 'backup-syncer',
                             'activity-log-exporter', 'environment-snapshot-refresher'
                         )
                     ),
    rule_id          INTEGER REFERENCES automation_rule (id) ON DELETE SET NULL,
    job_run_id       INTEGER REFERENCES job_run (id) ON DELETE SET NULL,
    environment_id   INTEGER REFERENCES environment (id) ON DELETE SET NULL,
    -- No CHECK on the object kind until the set of kinds stops growing.
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

CREATE INDEX idx_activity_event_occurred_at ON activity_event (occurred_at DESC);

CREATE INDEX idx_activity_event_category ON activity_event (category, occurred_at DESC);

CREATE INDEX idx_activity_event_target
    ON activity_event (environment_id, target_type, target_id, occurred_at DESC);

CREATE INDEX idx_activity_event_actor_subsystem
    ON activity_event (actor_subsystem, occurred_at DESC)
    WHERE actor_subsystem IS NOT NULL;

CREATE INDEX idx_activity_event_actor_user_id
    ON activity_event (actor_user_id, occurred_at DESC)
    WHERE actor_user_id IS NOT NULL;

CREATE TABLE object_storage_target (
    id                            INTEGER PRIMARY KEY,
    name                          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    endpoint_url                  TEXT NOT NULL,
    -- Required even where the provider ignores it, because SigV4 signs it.
    region                        TEXT NOT NULL,
    bucket                        TEXT NOT NULL,
    access_key_id                 TEXT NOT NULL,
    secret_access_key_ciphertext  TEXT NOT NULL,
    use_path_style                INTEGER NOT NULL CHECK (use_path_style IN (0, 1)),
    verify_tls                    INTEGER NOT NULL DEFAULT 1 CHECK (verify_tls IN (0, 1)),
    ca_bundle                     TEXT,
    backup_prefix                 TEXT NOT NULL,
    activity_log_prefix           TEXT NOT NULL,
    retention_count               INTEGER
                                  CHECK (retention_count IS NULL OR retention_count > 0),
    created_at                    TEXT NOT NULL,
    updated_at                    TEXT NOT NULL,
    -- Prefixes must not be shared: the backup retention sweep would delete log exports.
    CHECK (backup_prefix <> activity_log_prefix)
);

CREATE TABLE backup_record (
    id                       INTEGER PRIMARY KEY,
    object_storage_target_id INTEGER REFERENCES object_storage_target (id) ON DELETE SET NULL,
    created_at               TEXT NOT NULL,
    size_bytes               INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    key_version              INTEGER NOT NULL,
    key_fingerprint          TEXT NOT NULL,
    destination_key          TEXT,
    outcome                  TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
    error                    TEXT
);

CREATE INDEX idx_backup_record_created_at ON backup_record (created_at DESC);

CREATE INDEX idx_backup_record_target
    ON backup_record (object_storage_target_id, created_at DESC);
