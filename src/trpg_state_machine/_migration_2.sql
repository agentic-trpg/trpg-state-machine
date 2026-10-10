BEGIN IMMEDIATE;
CREATE TABLE command_receipts (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    command_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (session_id, command_id)
);
CREATE TABLE event_evidence (
    session_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (session_id, event_seq),
    FOREIGN KEY (session_id, event_seq) REFERENCES events(session_id, event_seq)
);
PRAGMA user_version = 2;
COMMIT;
