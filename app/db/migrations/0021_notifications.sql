-- Notification channels, the event types and environments each one receives, and the queue of
-- deliveries still to send.

-- AUTOINCREMENT: a channel id named by an activity event is never reissued.
CREATE TABLE notification_channel (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    kind                TEXT NOT NULL CHECK (
                            kind IN (
                                'webhook', 'ntfy', 'gotify', 'discord', 'slack', 'teams', 'email'
                            )
                        ),
    -- Non-secret fields only; secrets are sealed in secrets_ciphertext.
    settings            TEXT NOT NULL CHECK (json_valid(settings)),
    secrets_ciphertext  TEXT,
    is_enabled          INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    -- Applies only to events that name an environment; the listed environments are in notification_channel_environment.
    environment_scope   TEXT NOT NULL DEFAULT 'all' CHECK (environment_scope IN ('all', 'listed')),
    last_success_at     TEXT,
    last_error_at       TEXT,
    last_error          TEXT CHECK (last_error IS NULL OR length(last_error) <= 512),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_notification_channel_name ON notification_channel (lower(name));

CREATE TABLE notification_subscription (
    channel_id  INTEGER NOT NULL REFERENCES notification_channel (id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL CHECK (
                    length(event_type) BETWEEN 1 AND 64 AND event_type NOT GLOB '*[^a-z_]*'
                ),
    PRIMARY KEY (channel_id, event_type)
) WITHOUT ROWID;

CREATE TABLE notification_channel_environment (
    channel_id      INTEGER NOT NULL REFERENCES notification_channel (id) ON DELETE CASCADE,
    environment_id  INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    PRIMARY KEY (channel_id, environment_id)
) WITHOUT ROWID;

CREATE INDEX idx_notification_channel_environment_environment_id
    ON notification_channel_environment (environment_id);

CREATE TABLE notification_outbox (
    id               INTEGER PRIMARY KEY,
    channel_id       INTEGER NOT NULL REFERENCES notification_channel (id) ON DELETE CASCADE,
    event_id         INTEGER NOT NULL REFERENCES activity_event (id) ON DELETE CASCADE,
    event_type       TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    next_attempt_at  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (channel_id, event_id)
);

CREATE INDEX idx_notification_outbox_due ON notification_outbox (channel_id, next_attempt_at);

CREATE INDEX idx_notification_outbox_event_id ON notification_outbox (event_id);
