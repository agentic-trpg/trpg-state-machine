BEGIN IMMEDIATE;
DROP TRIGGER lock_insert_state;
DROP TRIGGER lock_update_state;
DROP TRIGGER locked_door_switch;
CREATE TABLE objects_w5 (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    object_id TEXT NOT NULL,
    scene_id TEXT,
    state TEXT CHECK(state IS NULL OR state IN ('closed','open')),
    cell TEXT,
    carrier_id TEXT,
    item_json TEXT CHECK(item_json IS NULL OR json_valid(item_json)),
    container_id TEXT,
    PRIMARY KEY(session_id,object_id),
    FOREIGN KEY(session_id,scene_id) REFERENCES scene_instances(session_id,scene_id),
    FOREIGN KEY(session_id,carrier_id) REFERENCES actors(session_id,actor_id),
    FOREIGN KEY(session_id,container_id) REFERENCES objects_w5(session_id,object_id),
    CHECK((scene_id IS NOT NULL AND carrier_id IS NULL AND container_id IS NULL)
       OR (scene_id IS NULL AND cell IS NULL AND carrier_id IS NOT NULL AND container_id IS NULL)
       OR (scene_id IS NULL AND cell IS NULL AND carrier_id IS NULL AND container_id IS NOT NULL)),
    CHECK(item_json IS NOT NULL OR (carrier_id IS NULL AND container_id IS NULL)),
    CHECK(item_json IS NULL OR (carrier_id IS NOT NULL OR container_id IS NOT NULL OR cell IS NOT NULL)),
    CHECK(container_id IS NULL OR container_id<>object_id)
);
INSERT INTO objects_w5(session_id,object_id,scene_id,state,cell,carrier_id,item_json)
    SELECT session_id,object_id,scene_id,state,cell,carrier_id,item_json FROM objects;
DROP TABLE objects;
ALTER TABLE objects_w5 RENAME TO objects;
CREATE INDEX objects_cell ON objects(session_id,scene_id,cell) WHERE cell IS NOT NULL;
CREATE INDEX objects_carrier ON objects(session_id,carrier_id) WHERE carrier_id IS NOT NULL;
CREATE INDEX objects_container ON objects(session_id,container_id) WHERE container_id IS NOT NULL;
CREATE TRIGGER lock_insert_state BEFORE INSERT ON door_locks
WHEN NOT EXISTS (SELECT 1 FROM objects WHERE session_id=NEW.session_id
    AND object_id=NEW.object_id AND state IN ('open','closed') AND (NEW.locked=0 OR state='closed'))
BEGIN SELECT RAISE(ABORT,'Invalid object lock state'); END;
CREATE TRIGGER lock_update_state BEFORE UPDATE ON door_locks
WHEN NOT EXISTS (SELECT 1 FROM objects WHERE session_id=NEW.session_id
    AND object_id=NEW.object_id AND state IN ('open','closed') AND (NEW.locked=0 OR state='closed'))
BEGIN SELECT RAISE(ABORT,'Invalid object lock state'); END;
CREATE TRIGGER locked_door_switch BEFORE UPDATE OF state ON objects
WHEN NEW.state IS NOT 'closed' AND EXISTS (SELECT 1 FROM door_locks
    WHERE session_id=OLD.session_id AND object_id=OLD.object_id AND locked=1)
BEGIN SELECT RAISE(ABORT,'Locked object must remain closed'); END;
CREATE TRIGGER object_container_cycle BEFORE UPDATE OF container_id ON objects
WHEN NEW.container_id IS NOT NULL AND EXISTS (
    WITH RECURSIVE parents(object_id,container_id) AS (
        SELECT object_id,container_id FROM objects
        WHERE session_id=NEW.session_id AND object_id=NEW.container_id
        UNION SELECT o.object_id,o.container_id FROM objects o JOIN parents p
        ON o.object_id=p.container_id WHERE o.session_id=NEW.session_id
    ) SELECT 1 FROM parents WHERE object_id=NEW.object_id
)
BEGIN SELECT RAISE(ABORT,'Container cycle'); END;
PRAGMA user_version = 29;
COMMIT;
