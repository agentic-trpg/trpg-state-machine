-- One durable window per original Bardic check. The receipt binds the roll event.
CREATE TABLE pending_bardic_checks (
    session_id TEXT NOT NULL,
    source_command_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    source_event_seq INTEGER NOT NULL,
    original_evidence_json TEXT NOT NULL CHECK(json_type(original_evidence_json) = 'object'),
    facts_fingerprint TEXT NOT NULL CHECK(length(facts_fingerprint) = 64),
    grant_origin TEXT NOT NULL,
    grant_id TEXT NOT NULL CHECK(length(grant_id) > 0),
    die_sides INTEGER NOT NULL CHECK(die_sides IN (6,8,10,12)),
    expires_tick INTEGER NOT NULL CHECK(expires_tick >= 0),
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','declined','expired','stale')),
    answer_command_id TEXT,
    PRIMARY KEY(session_id, source_command_id),
    CHECK((status = 'pending') = (answer_command_id IS NULL)),
    FOREIGN KEY(session_id,source_command_id) REFERENCES command_receipts(session_id,command_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id,source_event_seq) REFERENCES event_records(session_id,event_seq),
    FOREIGN KEY(session_id,answer_command_id) REFERENCES command_receipts(session_id,command_id)
        DEFERRABLE INITIALLY DEFERRED
);
PRAGMA user_version = 72;
