-- activity_event gains an index for reading one request id newest first.

CREATE INDEX idx_activity_event_request_id
    ON activity_event (request_id, occurred_at)
    WHERE request_id IS NOT NULL;
