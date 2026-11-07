-- Swarm stack names, one name across both stack kinds per environment, and the token that
-- versions a stack variable's value.

-- Aborts naming the colliding column before the unique index is created.
CREATE TABLE migration_guard (
    no_stack_names_differ_only_by_case INTEGER NOT NULL
        CHECK (no_stack_names_differ_only_by_case = 1)
);

INSERT INTO migration_guard (no_stack_names_differ_only_by_case)
SELECT CASE
    WHEN EXISTS (
        SELECT 1 FROM stack
        GROUP BY environment_id, lower(name)
        HAVING COUNT(*) > 1
    ) THEN 0
    ELSE 1
END;

DROP TABLE migration_guard;

-- The triggers stand in for a swarm name CHECK. A later rebuild of stack must recreate them.
CREATE UNIQUE INDEX idx_stack_name_folded ON stack (environment_id, lower(name));

-- The message begins with the text SQLite gives a CHECK failure, which callers match on.
CREATE TRIGGER stack_swarm_name_insert
BEFORE INSERT ON stack
WHEN NEW.kind = 'swarm' AND NOT (
    length(NEW.name) BETWEEN 1 AND 63
    AND NEW.name NOT GLOB '*[^A-Za-z0-9_-]*'
    AND substr(NEW.name, 1, 1) GLOB '[A-Za-z0-9]'
)
BEGIN
    SELECT RAISE(ABORT, 'CHECK constraint failed: stack swarm name');
END;

CREATE TRIGGER stack_swarm_name_update
BEFORE UPDATE OF name, kind ON stack
WHEN NEW.kind = 'swarm' AND NOT (
    length(NEW.name) BETWEEN 1 AND 63
    AND NEW.name NOT GLOB '*[^A-Za-z0-9_-]*'
    AND substr(NEW.name, 1, 1) GLOB '[A-Za-z0-9]'
)
BEGIN
    SELECT RAISE(ABORT, 'CHECK constraint failed: stack swarm name');
END;

ALTER TABLE stack_variable ADD COLUMN value_token TEXT CHECK (
    value_token IS NULL
    OR (length(value_token) = 12 AND value_token NOT GLOB '*[^0-9a-z]*')
);
