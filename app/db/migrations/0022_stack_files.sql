-- Companion files of a stack, and the digests that tell whether a stack's inputs changed since its
-- last deploy.

-- AUTOINCREMENT: a file id named by an activity event is never reissued.
CREATE TABLE stack_file (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    stack_id            INTEGER NOT NULL REFERENCES stack (id) ON DELETE CASCADE,
    name                TEXT NOT NULL CHECK (
                            length(name) BETWEEN 1 AND 128
                            AND name NOT GLOB '*[^A-Za-z0-9._-]*'
                            AND substr(name, 1, 1) GLOB '[A-Za-z0-9]'
                        ),
    is_secret           INTEGER NOT NULL CHECK (is_secret IN (0, 1)),
    content             TEXT CHECK (content IS NULL OR length(CAST(content AS BLOB)) BETWEEN 1 AND 65536),
    content_ciphertext  TEXT,
    -- Null for a secret file, whose content digest is never stored.
    content_digest      TEXT CHECK (
                            content_digest IS NULL
                            OR (length(content_digest) = 64 AND content_digest NOT GLOB '*[^0-9a-f]*')
                        ),
    size_bytes          INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND 65536),
    value_token         TEXT NOT NULL CHECK (length(value_token) = 12 AND value_token NOT GLOB '*[^0-9a-z]*'),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    CHECK (
        (is_secret = 0 AND content IS NOT NULL AND content_ciphertext IS NULL AND content_digest IS NOT NULL)
        OR (is_secret = 1 AND content IS NULL AND content_ciphertext IS NOT NULL AND content_digest IS NULL)
    )
);

CREATE UNIQUE INDEX idx_stack_file_name_folded ON stack_file (stack_id, lower(name));

ALTER TABLE stack ADD COLUMN inputs_digest TEXT CHECK (
    inputs_digest IS NULL
    OR (length(inputs_digest) = 64 AND inputs_digest NOT GLOB '*[^0-9a-f]*')
);

ALTER TABLE stack ADD COLUMN deployed_inputs_digest TEXT CHECK (
    deployed_inputs_digest IS NULL
    OR (length(deployed_inputs_digest) = 64 AND deployed_inputs_digest NOT GLOB '*[^0-9a-f]*')
);

UPDATE stack_variable SET value_token = lower(hex(randomblob(6)))
    WHERE is_secret = 1 AND value_token IS NULL;

ALTER TABLE stack DROP COLUMN variables_changed_at;
