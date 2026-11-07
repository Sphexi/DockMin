-- stack_build_context: build context archives uploaded to a Compose stack, stored as uploaded.

CREATE TABLE stack_build_context (
    stack_id        INTEGER NOT NULL REFERENCES stack (id) ON DELETE CASCADE,
    name            TEXT NOT NULL COLLATE NOCASE CHECK (length(name) BETWEEN 1 AND 128),
    content         BLOB NOT NULL CHECK (length(content) BETWEEN 1 AND 16777216),
    content_digest  TEXT NOT NULL CHECK (length(content_digest) = 64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    compressed      INTEGER NOT NULL CHECK (compressed IN (0, 1)),
    member_count    INTEGER NOT NULL CHECK (member_count BETWEEN 1 AND 10000),
    extracted_size  INTEGER NOT NULL CHECK (extracted_size BETWEEN 1 AND 268435456),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (stack_id, name)
) WITHOUT ROWID;
