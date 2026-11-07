-- Ascending: a backward scan yields (occurred_at, rowid) both descending, the keyset order.
DROP INDEX idx_activity_event_occurred_at;
CREATE INDEX idx_activity_event_occurred_at ON activity_event (occurred_at);

DROP INDEX idx_activity_event_category;
CREATE INDEX idx_activity_event_category ON activity_event (category, occurred_at);

DROP INDEX idx_activity_event_target;
CREATE INDEX idx_activity_event_target
    ON activity_event (environment_id, target_type, target_id, occurred_at);

DROP INDEX idx_activity_event_actor_subsystem;
CREATE INDEX idx_activity_event_actor_subsystem
    ON activity_event (actor_subsystem, occurred_at)
    WHERE actor_subsystem IS NOT NULL;

DROP INDEX idx_activity_event_actor_user_id;
CREATE INDEX idx_activity_event_actor_user_id
    ON activity_event (actor_user_id, occurred_at)
    WHERE actor_user_id IS NOT NULL;

-- Not partial: a target filter with no environment matches environment_id IS NULL.
CREATE INDEX idx_activity_event_environment ON activity_event (environment_id, occurred_at);

CREATE INDEX idx_activity_event_rule_id
    ON activity_event (rule_id, occurred_at)
    WHERE rule_id IS NOT NULL;

CREATE INDEX idx_activity_event_job_run_id
    ON activity_event (job_run_id)
    WHERE job_run_id IS NOT NULL;
