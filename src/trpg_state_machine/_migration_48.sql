-- Fresh-development Schema48: ordered independent actor conditions.
CREATE TABLE character_conditions (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    condition TEXT NOT NULL CHECK(typeof(condition)='text'),
    source_entity_id TEXT NOT NULL CHECK(typeof(source_entity_id)='text'),
    scope TEXT NOT NULL CHECK(scope IN ('combat','session')),
    applied_round INTEGER NOT NULL CHECK(typeof(applied_round)='integer'),
    duration_rounds INTEGER CHECK(duration_rounds IS NULL OR typeof(duration_rounds)='integer'),
    save_dc INTEGER CHECK(save_dc IS NULL OR typeof(save_dc)='integer'),
    exhaustion_level INTEGER NOT NULL CHECK(typeof(exhaustion_level)='integer'),
    ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal >= 0),
    PRIMARY KEY(session_id, actor_id, condition_id),
    FOREIGN KEY(session_id,actor_id) REFERENCES mechanical_actors(session_id,actor_id)
        ON DELETE CASCADE
);
CREATE INDEX character_conditions_order ON character_conditions(session_id,actor_id,ordinal);
PRAGMA user_version = 48;
