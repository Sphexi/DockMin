-- How each backup record was produced, the digest of its archive and the schema it holds.

-- A null destination means either a download or a removed target; origin tells them apart.
ALTER TABLE backup_record ADD COLUMN origin TEXT NOT NULL DEFAULT 'sync'
    CHECK (origin IN ('download', 'sync'));

ALTER TABLE backup_record ADD COLUMN archive_sha256 TEXT
    CHECK (
        archive_sha256 IS NULL
        OR (length(archive_sha256) = 64 AND archive_sha256 NOT GLOB '*[^0-9a-f]*')
    );

ALTER TABLE backup_record ADD COLUMN schema_version INTEGER
    CHECK (schema_version IS NULL OR schema_version >= 1);
