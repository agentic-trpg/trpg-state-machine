-- Fresh-development Schema44: each owned resource balance has one keyed fact row.
ALTER TABLE characters DROP COLUMN resources_json;
ALTER TABLE mechanical_components DROP COLUMN resources_json;
ALTER TABLE mechanical_components ADD COLUMN resource_state_present INTEGER NOT NULL DEFAULT 0
    CHECK(resource_state_present IN (0, 1));
CREATE TABLE character_resources (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'synthetic', 'hit_dice', 'spell_slot', 'pact_slot', 'feature_uses',
        'standalone_spell_slot', 'standalone_pact_slot'
    )),
    current INTEGER NOT NULL CHECK(current >= 0),
    maximum INTEGER CHECK(maximum IS NULL OR (maximum >= 0 AND current <= maximum)),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    PRIMARY KEY(session_id, actor_id, resource_id),
    FOREIGN KEY(session_id, actor_id) REFERENCES characters(session_id, actor_id)
);
CREATE INDEX character_resources_owner_order
    ON character_resources(session_id, actor_id, kind, ordinal);
PRAGMA user_version = 44;
