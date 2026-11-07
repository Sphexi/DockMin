-- Null where the host could not report the count, never zero.
ALTER TABLE environment_snapshot
    ADD COLUMN container_paused_count INTEGER
    CHECK (container_paused_count IS NULL OR container_paused_count >= 0);

ALTER TABLE environment_snapshot
    ADD COLUMN container_unhealthy_count INTEGER
    CHECK (container_unhealthy_count IS NULL OR container_unhealthy_count >= 0);
