-- Fresh-development Schema46: one concentration owner and keyed target effects.
ALTER TABLE mechanical_components DROP COLUMN effects_json;
CREATE TABLE effect_concentrations (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    concentration_id TEXT NOT NULL CHECK(length(concentration_id) > 0),
    caster_id TEXT NOT NULL,
    effect_id TEXT NOT NULL CHECK(length(effect_id) > 0),
    rounds_remaining INTEGER NOT NULL CHECK(rounds_remaining >= 0),
    PRIMARY KEY(session_id, concentration_id),
    UNIQUE(session_id, caster_id),
    FOREIGN KEY(session_id, caster_id) REFERENCES mechanical_actors(session_id, actor_id)
);
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
    PRIMARY KEY(session_id, target_id, effect_id, origin),
    UNIQUE(session_id, ordinal),
    UNIQUE(session_id, concentration_id, chain_ordinal),
    FOREIGN KEY(session_id, target_id) REFERENCES mechanical_actors(session_id, actor_id),
    FOREIGN KEY(session_id, concentration_id)
        REFERENCES effect_concentrations(session_id, concentration_id),
    CHECK((concentration_id IS NULL) = (chain_ordinal IS NULL))
);
PRAGMA user_version = 46;
