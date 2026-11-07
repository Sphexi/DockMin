-- session remembers the environment its navigation last scoped to.

ALTER TABLE session ADD COLUMN nav_environment_id INTEGER REFERENCES environment (id) ON DELETE SET NULL;
