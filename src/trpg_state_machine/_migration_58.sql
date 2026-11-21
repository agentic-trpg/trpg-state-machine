-- Exploration spell prompts share the Session command transaction and receipt ledger.
CREATE TABLE pending_spell_choices (
    session_id TEXT NOT NULL,
    source_command_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    responder_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    input_world_version INTEGER NOT NULL CHECK(input_world_version >= 0),
    created_tick INTEGER NOT NULL CHECK(created_tick >= 0),
    expires_tick INTEGER NOT NULL CHECK(expires_tick > created_tick),
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','declined','expired','rejected')),
    answer_command_id TEXT,
    PRIMARY KEY(session_id, source_command_id),
    CHECK((status = 'pending') = (answer_command_id IS NULL)),
    FOREIGN KEY(session_id,source_command_id) REFERENCES command_receipts(session_id,command_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id,answer_command_id) REFERENCES command_receipts(session_id,command_id)
        DEFERRABLE INITIALLY DEFERRED
);
PRAGMA user_version = 58;
