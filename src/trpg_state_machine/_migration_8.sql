BEGIN IMMEDIATE;
CREATE TABLE trigger_history_new (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_definition_id TEXT NOT NULL,
    source_command_id TEXT NOT NULL,
    source_event_seq INTEGER,
    source_event_status TEXT NOT NULL CHECK(source_event_status IN ('linked','legacy_unavailable')),
    PRIMARY KEY(session_id,event_definition_id),
    FOREIGN KEY(session_id,source_event_seq) REFERENCES event_records(session_id,event_seq)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK ((source_event_status='linked' AND source_event_seq IS NOT NULL)
        OR (source_event_status='legacy_unavailable' AND source_event_seq IS NULL))
);
INSERT INTO trigger_history_new
SELECT session_id,event_definition_id,source_command_id,NULL,'legacy_unavailable' FROM trigger_history;
DROP TABLE trigger_history;
ALTER TABLE trigger_history_new RENAME TO trigger_history;
PRAGMA user_version = 8;
COMMIT;
