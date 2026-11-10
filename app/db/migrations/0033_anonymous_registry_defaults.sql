-- registry_credential.seeded_default marks the two anonymous entries a new instance starts with.

ALTER TABLE registry_credential
    ADD COLUMN seeded_default INTEGER NOT NULL DEFAULT 0 CHECK (seeded_default IN (0, 1));

-- Seeded only into a database with no user and no registry set; no other database is touched.
INSERT INTO registry_credential (
    name, provider, url, authentication_enabled, secret_kind, provider_extras,
    seeded_default, created_at, updated_at
)
SELECT
    defaults.name, defaults.provider, defaults.url, 0, 'static', defaults.provider_extras,
    1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
FROM (
    SELECT 'Docker Hub (anonymous)' AS name, 'dockerhub' AS provider, 'docker.io' AS url,
           '{}' AS provider_extras
    UNION ALL
    SELECT 'GitHub Container Registry (anonymous)', 'ghcr', 'ghcr.io',
           '{"organisation":null,"use_organisation":false}'
) AS defaults
WHERE NOT EXISTS (SELECT 1 FROM user)
  AND NOT EXISTS (SELECT 1 FROM registry_credential);
