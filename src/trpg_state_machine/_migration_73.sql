-- Character owns exact worn accessory bindings; World owns item attunement.
CREATE TABLE character_worn_items (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    item_slug TEXT NOT NULL,
    PRIMARY KEY(session_id,actor_id,instance_id),
    UNIQUE(session_id,instance_id),
    FOREIGN KEY(session_id,actor_id) REFERENCES actors(session_id,actor_id),
    FOREIGN KEY(session_id,instance_id) REFERENCES objects(session_id,object_id)
);
CREATE INDEX objects_attuned_actor ON objects(session_id,item_attuned_actor_id,object_id)
    WHERE item_attuned_actor_id IS NOT NULL;
PRAGMA user_version = 73;
