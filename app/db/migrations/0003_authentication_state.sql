-- Idle-timeout tracking for sessions, and the failure counters behind login lockout.

-- Null until the first request after issue; readers treat null as issued_at.
ALTER TABLE session ADD COLUMN last_seen_at TEXT;

-- subject is the lower-cased attempted username for kind 'username', the remote address for kind 'address'.
CREATE TABLE login_throttle (
    id                 INTEGER PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('username', 'address')),
    subject            TEXT NOT NULL,
    failure_count      INTEGER NOT NULL CHECK (failure_count >= 0),
    window_started_at  TEXT NOT NULL,
    locked_until       TEXT,
    UNIQUE (kind, subject)
);

CREATE INDEX idx_login_throttle_window_started_at ON login_throttle (window_started_at);
