-- Fresh-development Schema50: stable per-actor attributes and ordered proficiency facts.
CREATE TABLE character_attributes (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    strength INTEGER NOT NULL,
    dexterity INTEGER NOT NULL,
    constitution INTEGER NOT NULL,
    intelligence INTEGER NOT NULL,
    wisdom INTEGER NOT NULL,
    charisma INTEGER NOT NULL,
    ac INTEGER NOT NULL,
    character_level INTEGER NOT NULL,
    proficiency_bonus_override INTEGER,
    darkvision INTEGER,
    blindsight INTEGER,
    tremorsense INTEGER,
    truesight INTEGER,
    weapon_proficiencies_present INTEGER NOT NULL CHECK(weapon_proficiencies_present IN (0,1)),
    PRIMARY KEY(session_id,actor_id),
    FOREIGN KEY(session_id,actor_id) REFERENCES mechanical_actors(session_id,actor_id) ON DELETE CASCADE
);
CREATE TABLE character_proficiencies (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('save','skill','tool','weapon')),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    proficiency_id TEXT NOT NULL,
    proficient INTEGER NOT NULL CHECK(proficient IN (0,1)),
    expertise_ordinal INTEGER,
    PRIMARY KEY(session_id,actor_id,kind,ordinal),
    FOREIGN KEY(session_id,actor_id) REFERENCES character_attributes(session_id,actor_id) ON DELETE CASCADE
);
CREATE INDEX character_proficiencies_identity ON character_proficiencies(session_id,actor_id,kind,proficiency_id);
PRAGMA user_version = 50;
