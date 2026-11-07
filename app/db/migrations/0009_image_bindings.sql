-- Image credential bindings that outlive their credential set, and one default credential set
-- per registry address.

-- Rebuild: SQLite cannot change a CHECK in place. The guard aborts if the rebuild would discard a row.
CREATE TABLE migration_guard (
    binding_table_is_empty INTEGER NOT NULL CHECK (binding_table_is_empty = 1)
);

INSERT INTO migration_guard (binding_table_is_empty)
SELECT CASE WHEN EXISTS (SELECT 1 FROM image_credential_binding) THEN 0 ELSE 1 END;

DROP TABLE migration_guard;

-- AUTOINCREMENT: a binding id named by a stale reap confirmation never matches another binding.
CREATE TABLE image_credential_binding_new (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id           INTEGER NOT NULL REFERENCES environment (id) ON DELETE CASCADE,
    image_reference          TEXT NOT NULL CHECK (length(image_reference) BETWEEN 1 AND 512),
    -- RESTRICT: a set is deleted only after its bindings move to credential_missing.
    registry_credential_id   INTEGER REFERENCES registry_credential (id) ON DELETE RESTRICT,
    -- No foreign key: it names a set that no longer exists, and set ids are never reissued.
    missing_credential_id    INTEGER,
    missing_credential_name  TEXT CHECK (
                                 missing_credential_name IS NULL
                                 OR length(missing_credential_name) BETWEEN 1 AND 64
                             ),
    missing_reason           TEXT CHECK (
                                 missing_reason IS NULL
                                 OR missing_reason IN ('deleted', 'address_changed')
                             ),
    observed_digest          TEXT CHECK (
                                 observed_digest IS NULL
                                 OR (
                                     length(observed_digest) = 71
                                     AND observed_digest GLOB 'sha256:*'
                                     AND substr(observed_digest, 8) NOT GLOB '*[^0-9a-f]*'
                                 )
                             ),
    state                    TEXT NOT NULL CHECK (state IN ('bound', 'credential_missing')),
    bound_at                 TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    credential_missing_at    TEXT,
    UNIQUE (environment_id, image_reference),
    CHECK (
        (
            state = 'bound'
            AND missing_credential_id IS NULL
            AND missing_credential_name IS NULL
            AND missing_reason IS NULL
            AND credential_missing_at IS NULL
        )
        OR (
            state = 'credential_missing'
            AND registry_credential_id IS NULL
            AND missing_credential_id IS NOT NULL
            AND missing_credential_name IS NOT NULL
            AND missing_reason IS NOT NULL
            AND credential_missing_at IS NOT NULL
        )
    )
);

DROP TABLE image_credential_binding;

ALTER TABLE image_credential_binding_new RENAME TO image_credential_binding;

CREATE INDEX idx_image_credential_binding_registry_credential_id
    ON image_credential_binding (registry_credential_id, environment_id, image_reference)
    WHERE registry_credential_id IS NOT NULL;

CREATE UNIQUE INDEX idx_registry_credential_default_for_host
    ON registry_credential (url)
    WHERE is_default_for_host = 1;
