CREATE TABLE pending_shield_windows (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    source_command_id TEXT NOT NULL,
    attacker_id TEXT NOT NULL,
    defender_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    input_world_version INTEGER NOT NULL CHECK (input_world_version >= 0),
    checkpoint_token TEXT NOT NULL CHECK (length(checkpoint_token) = 64),
    process_id TEXT NOT NULL,
    CHECK (attacker_id <> defender_id),
    FOREIGN KEY (session_id, source_command_id)
        REFERENCES command_receipts(session_id, command_id) DEFERRABLE INITIALLY DEFERRED
);
PRAGMA user_version = 60;
