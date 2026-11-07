-- Stack controls are matched on name with case folded; object ids of other types stay case-sensitive.

-- Aborts naming the colliding column before the unique index is created.
CREATE TABLE migration_guard (
    no_stack_controls_differ_only_by_case INTEGER NOT NULL
        CHECK (no_stack_controls_differ_only_by_case = 1)
);

INSERT INTO migration_guard (no_stack_controls_differ_only_by_case)
SELECT CASE
    WHEN EXISTS (
        SELECT 1 FROM resource_control
        WHERE resource_type = 'stack'
        GROUP BY environment_id, lower(resource_id)
        HAVING COUNT(*) > 1
    ) THEN 0
    ELSE 1
END;

DROP TABLE migration_guard;

CREATE UNIQUE INDEX idx_resource_control_stack_name_folded
    ON resource_control (environment_id, lower(resource_id))
    WHERE resource_type = 'stack';
