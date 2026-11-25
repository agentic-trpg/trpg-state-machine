-- Preserve cast lineage for both elapsed and recipient-long-rest spell lifetimes.
ALTER TABLE ongoing_effects RENAME TO previous_ongoing_effects;
CREATE TABLE ongoing_effects (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    target_id TEXT NOT NULL,
    effect_id TEXT NOT NULL CHECK(length(effect_id) > 0),
    origin TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    concentration_id TEXT,
    chain_ordinal INTEGER CHECK(chain_ordinal IS NULL OR chain_ordinal >= 0),
    conditions_json TEXT CHECK(conditions_json IS NULL OR json_valid(conditions_json)),
    lifecycle_json TEXT CHECK(lifecycle_json IS NULL OR json_valid(lifecycle_json)),
    expires_tick INTEGER CHECK(expires_tick IS NULL OR (typeof(expires_tick)='integer' AND expires_tick>=0)),
    cast_by TEXT,
    cast_spell_id TEXT,
    cast_command_id TEXT,
    cast_slot_level INTEGER CHECK(cast_slot_level IS NULL OR cast_slot_level BETWEEN 0 AND 9),
    PRIMARY KEY(session_id, target_id, effect_id, origin),
    UNIQUE(session_id, ordinal),
    UNIQUE(session_id, concentration_id, chain_ordinal),
    FOREIGN KEY(session_id, target_id) REFERENCES mechanical_actors(session_id, actor_id),
    FOREIGN KEY(session_id, concentration_id)
        REFERENCES effect_concentrations(session_id, concentration_id),
    CHECK((concentration_id IS NULL) = (chain_ordinal IS NULL)),
    CHECK (
        (cast_by IS NULL AND cast_spell_id IS NULL AND cast_command_id IS NULL)
        OR (cast_by IS NOT NULL AND length(cast_by) > 0
            AND cast_spell_id IS NOT NULL AND length(cast_spell_id) > 0
            AND cast_command_id IS NOT NULL AND length(cast_command_id) > 0
            AND (expires_tick IS NOT NULL
                OR json_type(payload_json, '$.duration.until_long_rest') IS 'true'))
    ),
    CHECK (
        json_type(payload_json, '$.duration.until_long_rest') IS NOT 'true'
        OR (cast_by IS NOT NULL AND expires_tick IS NULL AND concentration_id IS NULL)
    )
);
INSERT INTO ongoing_effects SELECT * FROM previous_ongoing_effects;
DROP TABLE previous_ongoing_effects;
CREATE INDEX ongoing_effects_world_deadline
    ON ongoing_effects(session_id,expires_tick,target_id,ordinal)
    WHERE expires_tick IS NOT NULL;
PRAGMA user_version = 76;
