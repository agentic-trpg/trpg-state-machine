BEGIN IMMEDIATE;
CREATE TABLE event_records (
    session_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    source_command_id TEXT NOT NULL,
    signals_json TEXT NOT NULL,
    PRIMARY KEY (session_id, event_seq),
    FOREIGN KEY (session_id, event_seq) REFERENCES events(session_id, event_seq)
);
INSERT INTO event_records
SELECT session_id, event_seq, 'event.' || session_id || '.' || event_seq,
       'legacy.unavailable', '[]' FROM events;
CREATE TABLE event_outbox (
    session_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    consumer TEXT NOT NULL CHECK (consumer IN ('delivery', 'perception')),
    done INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (session_id, event_seq, consumer),
    FOREIGN KEY (session_id, event_seq) REFERENCES event_records(session_id, event_seq)
);
INSERT INTO event_outbox SELECT session_id, event_seq, 'delivery', 0, 0, NULL FROM events;
CREATE TABLE projection_cursors (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    consumer TEXT NOT NULL,
    event_seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, consumer)
);
CREATE TABLE trigger_history (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_definition_id TEXT NOT NULL,
    source_command_id TEXT NOT NULL,
    PRIMARY KEY (session_id, event_definition_id)
);
PRAGMA user_version = 3;
COMMIT;
