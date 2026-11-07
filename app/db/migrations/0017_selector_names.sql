-- Container, stack and service selectors name a list of targets.

UPDATE automation_rule
SET target_selector = json_object(
    'kind', json_extract(target_selector, '$.kind'),
    'names', json_array(json_extract(target_selector, '$.name'))
)
-- json_extract raises on text that is not JSON; CASE keeps it from being reached.
WHERE CASE
    WHEN json_valid(target_selector) THEN
        json_extract(target_selector, '$.kind') IN ('container', 'stack', 'service')
        AND json_type(target_selector, '$.name') = 'text'
        AND json_type(target_selector, '$.names') IS NULL
    ELSE 0
END;

CREATE TABLE migration_guard (
    every_name_selector_holds_a_list INTEGER NOT NULL
        CHECK (every_name_selector_holds_a_list = 1)
);

INSERT INTO migration_guard (every_name_selector_holds_a_list)
SELECT CASE
    WHEN EXISTS (
        SELECT 1 FROM automation_rule
        WHERE CASE
            WHEN json_valid(target_selector) THEN
                json_extract(target_selector, '$.kind') IN ('container', 'stack', 'service')
                AND (
                    json_type(target_selector, '$.names') IS NOT 'array'
                    OR json_type(target_selector, '$.name') IS NOT NULL
                )
            ELSE 0
        END
    ) THEN 0
    ELSE 1
END;

DROP TABLE migration_guard;
