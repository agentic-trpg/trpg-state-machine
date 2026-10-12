BEGIN IMMEDIATE;
CREATE TABLE scene_instances_gc1 (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    scene_id TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    visited_count INTEGER NOT NULL CHECK(visited_count >= 0),
    PRIMARY KEY(session_id, scene_id)
);
INSERT INTO scene_instances_gc1 SELECT * FROM scene_instances;
DROP TABLE scene_instances;
ALTER TABLE scene_instances_gc1 RENAME TO scene_instances;
CREATE TABLE objects_gc1 (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    state TEXT CHECK(state IS NULL OR state IN ('closed', 'open')),
    cell TEXT,
    PRIMARY KEY(session_id, object_id),
    FOREIGN KEY(session_id, scene_id) REFERENCES scene_instances(session_id, scene_id)
);
INSERT INTO objects_gc1(session_id,object_id,scene_id,state)
    SELECT session_id,object_id,scene_id,state FROM objects;
DROP TABLE objects;
ALTER TABLE objects_gc1 RENAME TO objects;
CREATE INDEX objects_cell ON objects(session_id,scene_id,cell) WHERE cell IS NOT NULL;
ALTER TABLE actors ADD COLUMN cell TEXT;
ALTER TABLE mechanical_scenes ADD COLUMN spatial_enabled INTEGER NOT NULL DEFAULT 0 CHECK(spatial_enabled IN (0,1));
PRAGMA user_version = 21;
COMMIT;
