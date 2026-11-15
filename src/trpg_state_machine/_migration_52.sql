-- Fresh-development Schema52: exact /19 equipment and its actor state.
CREATE TABLE character_equipment (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    held_weapon_instance_id TEXT,
    worn_armor_instance_id TEXT,
    equipped_shield_instance_id TEXT,
    offhand_item_instance_id TEXT,
    weapon_grip TEXT NOT NULL CHECK (weapon_grip IN ('none','one_handed','two_handed')),
    stealth_disadvantage INTEGER NOT NULL CHECK (stealth_disadvantage IN (0,1)),
    legacy_weapon_slug TEXT CHECK (legacy_weapon_slug IS NULL OR held_weapon_instance_id IS NULL),
    PRIMARY KEY(session_id,actor_id),
    FOREIGN KEY(session_id,actor_id) REFERENCES mechanical_actors(session_id,actor_id)
        ON DELETE CASCADE,
    FOREIGN KEY(session_id,held_weapon_instance_id) REFERENCES objects(session_id,object_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id,worn_armor_instance_id) REFERENCES objects(session_id,object_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id,equipped_shield_instance_id) REFERENCES objects(session_id,object_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id,offhand_item_instance_id) REFERENCES objects(session_id,object_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX character_held_weapon_instance
    ON character_equipment(session_id,held_weapon_instance_id)
    WHERE held_weapon_instance_id IS NOT NULL;
CREATE INDEX character_worn_armor_instance
    ON character_equipment(session_id,worn_armor_instance_id)
    WHERE worn_armor_instance_id IS NOT NULL;
CREATE INDEX character_equipped_shield_instance
    ON character_equipment(session_id,equipped_shield_instance_id)
    WHERE equipped_shield_instance_id IS NOT NULL;
CREATE INDEX character_offhand_item_instance
    ON character_equipment(session_id,offhand_item_instance_id)
    WHERE offhand_item_instance_id IS NOT NULL;
PRAGMA user_version = 52;
