-- The latest update check result for each image reference on each environment.

CREATE TABLE image_update_status (
    environment_id   INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    image_reference  TEXT NOT NULL CHECK (length(image_reference) BETWEEN 1 AND 512),
    state            TEXT NOT NULL CHECK (
                         state IN ('up_to_date', 'update_available', 'unknown', 'pinned', 'not_bound')
                     ),
    reason           TEXT CHECK (
                         reason IS NULL
                         OR (length(reason) BETWEEN 1 AND 64 AND reason NOT GLOB '*[^a-z_]*')
                     ),
    local_digest     TEXT CHECK (
                         local_digest IS NULL
                         OR (
                             length(local_digest) = 71
                             AND local_digest GLOB 'sha256:*'
                             AND substr(local_digest, 8) NOT GLOB '*[^0-9a-f]*'
                         )
                     ),
    remote_digest    TEXT CHECK (
                         remote_digest IS NULL
                         OR (
                             length(remote_digest) = 71
                             AND remote_digest GLOB 'sha256:*'
                             AND substr(remote_digest, 8) NOT GLOB '*[^0-9a-f]*'
                         )
                     ),
    -- Compared as text, so only the stored form fits.
    checked_at       TEXT NOT NULL CHECK (
                         length(checked_at) = 20
                         AND checked_at GLOB
                             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                     ),
    PRIMARY KEY (environment_id, image_reference),
    CHECK ((state = 'unknown') = (reason IS NOT NULL)),
    CHECK (state NOT IN ('up_to_date', 'update_available') OR remote_digest IS NOT NULL)
) WITHOUT ROWID;
