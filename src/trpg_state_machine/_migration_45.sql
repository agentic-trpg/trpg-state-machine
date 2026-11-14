-- Fresh-development Schema45: keyed character casting facts replace the aggregate.
ALTER TABLE mechanical_components DROP COLUMN casting_json;
ALTER TABLE mechanical_components ADD COLUMN casting_state_present INTEGER NOT NULL DEFAULT 0
    CHECK(casting_state_present IN (0, 1));
CREATE TABLE character_casting (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    verbal_available INTEGER NOT NULL CHECK(verbal_available IN (0, 1)),
    PRIMARY KEY(session_id, actor_id),
    UNIQUE(session_id, ordinal),
    FOREIGN KEY(session_id, actor_id) REFERENCES mechanical_actors(session_id, actor_id)
);
CREATE TABLE character_spell_sources (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    source_id TEXT NOT NULL CHECK(length(source_id) > 0),
    class_slug TEXT NOT NULL CHECK(length(class_slug) > 0),
    ability TEXT NOT NULL CHECK(ability IN ('str','dex','con','int','wis','cha')),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    PRIMARY KEY(session_id, actor_id, source_id),
    UNIQUE(session_id, actor_id, class_slug),
    UNIQUE(session_id, actor_id, ordinal),
    FOREIGN KEY(session_id, actor_id) REFERENCES character_casting(session_id, actor_id)
);
CREATE TABLE character_spells (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    spell_slug TEXT NOT NULL CHECK(length(spell_slug) > 0),
    known_ordinal INTEGER NOT NULL CHECK(known_ordinal >= 0),
    prepared_ordinal INTEGER CHECK(prepared_ordinal IS NULL OR prepared_ordinal >= 0),
    PRIMARY KEY(session_id, actor_id, source_id, spell_slug),
    UNIQUE(session_id, actor_id, source_id, known_ordinal),
    UNIQUE(session_id, actor_id, source_id, prepared_ordinal),
    FOREIGN KEY(session_id, actor_id, source_id)
        REFERENCES character_spell_sources(session_id, actor_id, source_id)
);
CREATE TABLE character_spell_materials (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    spell_slug TEXT NOT NULL CHECK(spell_slug = 'hold-person'),
    inventory_instance_id TEXT NOT NULL CHECK(length(inventory_instance_id) > 0),
    available INTEGER NOT NULL CHECK(available IN (0, 1)),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    PRIMARY KEY(session_id, actor_id, source_id, spell_slug),
    UNIQUE(session_id, actor_id, ordinal),
    FOREIGN KEY(session_id, actor_id, source_id)
        REFERENCES character_spell_sources(session_id, actor_id, source_id)
);
CREATE TABLE character_spell_order (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    source_id TEXT NOT NULL,
    spell_slug TEXT NOT NULL,
    PRIMARY KEY(session_id, actor_id, ordinal),
    FOREIGN KEY(session_id, actor_id, source_id, spell_slug)
        REFERENCES character_spells(session_id, actor_id, source_id, spell_slug)
);
CREATE INDEX character_spell_materials_object
    ON character_spell_materials(session_id, actor_id, inventory_instance_id);
PRAGMA user_version = 45;
