-- template_catalogue: remote app template catalogues an administrator has added.

CREATE TABLE template_catalogue (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(name) BETWEEN 1 AND 64),
    kind            TEXT NOT NULL CHECK (kind IN ('linuxserver', 'portainer')),
    url             TEXT NOT NULL CHECK (length(url) BETWEEN 9 AND 2048 AND url LIKE 'https://%'),
    is_enabled      INTEGER NOT NULL DEFAULT 0 CHECK (is_enabled IN (0, 1)),
    content         TEXT,
    content_digest  TEXT CHECK (content_digest IS NULL
                                OR (length(content_digest) = 64 AND content_digest NOT GLOB '*[^0-9a-f]*')),
    entry_count     INTEGER NOT NULL DEFAULT 0 CHECK (entry_count BETWEEN 0 AND 2000),
    fetched_at      TEXT,
    -- A product error code, never text from the remote server.
    last_failure    TEXT CHECK (last_failure IS NULL OR length(last_failure) <= 256),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK (content IS NULL OR length(CAST(content AS BLOB)) <= 4194304)
);
