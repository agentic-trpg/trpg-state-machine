BEGIN IMMEDIATE;
-- Existing object facts are preserved. Only explicitly configured new instances
-- may occupy a carrier instead of a world Scene/Cell.
CREATE TABLE objects_gc3 (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    object_id TEXT NOT NULL,
    scene_id TEXT,
    state TEXT CHECK(state IS NULL OR state IN ('closed','open')),
    cell TEXT,
    carrier_id TEXT,
    item_json TEXT CHECK(item_json IS NULL OR json_valid(item_json)),
    PRIMARY KEY(session_id,object_id),
    FOREIGN KEY(session_id,scene_id) REFERENCES scene_instances(session_id,scene_id),
    FOREIGN KEY(session_id,carrier_id) REFERENCES actors(session_id,actor_id),
    CHECK((carrier_id IS NULL AND scene_id IS NOT NULL)
       OR (carrier_id IS NOT NULL AND scene_id IS NULL AND cell IS NULL)),
    CHECK(item_json IS NOT NULL OR carrier_id IS NULL),
    CHECK(item_json IS NULL OR (state IS NULL AND
        (carrier_id IS NOT NULL OR cell IS NOT NULL)))
);
INSERT INTO objects_gc3(session_id,object_id,scene_id,state,cell)
    SELECT session_id,object_id,scene_id,state,cell FROM objects;
DROP TABLE objects;
ALTER TABLE objects_gc3 RENAME TO objects;
CREATE INDEX objects_cell ON objects(session_id,scene_id,cell) WHERE cell IS NOT NULL;
CREATE INDEX objects_carrier ON objects(session_id,carrier_id) WHERE carrier_id IS NOT NULL;
PRAGMA user_version = 24;
COMMIT;
