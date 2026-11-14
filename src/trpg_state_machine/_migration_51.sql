-- Fresh-development Schema51: durable identity of an exactly consumed item.
CREATE TABLE consumed_items (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    authored_scene_id TEXT,
    PRIMARY KEY(session_id,object_id),
    UNIQUE(session_id,command_id,object_id),
    FOREIGN KEY(session_id,command_id) REFERENCES command_receipts(session_id,command_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id,authored_scene_id)
        REFERENCES scene_instances(session_id,scene_id)
);
CREATE INDEX door_credentials_instance ON door_credentials(session_id,instance_id);
CREATE INDEX door_locks_object ON door_locks(session_id,object_id);
CREATE INDEX character_spell_materials_instance
    ON character_spell_materials(session_id,inventory_instance_id);
CREATE TRIGGER consumed_item_identity_guard BEFORE INSERT ON objects
WHEN EXISTS (SELECT 1 FROM consumed_items WHERE session_id=NEW.session_id AND object_id=NEW.object_id)
BEGIN SELECT RAISE(ABORT,'Consumed item identity cannot be recreated'); END;
PRAGMA user_version = 51;
