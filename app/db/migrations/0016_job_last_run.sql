-- The latest run of a job that leaves no job_run row: a successful scheduled run of a job that
-- writes no activity events.
ALTER TABLE scheduled_job ADD COLUMN last_run_at TEXT;

ALTER TABLE scheduled_job ADD COLUMN last_run_outcome TEXT
    CHECK (last_run_outcome IS NULL OR last_run_outcome = 'success');
