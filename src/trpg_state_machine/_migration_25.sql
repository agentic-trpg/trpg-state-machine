BEGIN IMMEDIATE;
CREATE TABLE door_locks (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    locked INTEGER NOT NULL CHECK (locked IN (0,1)),
    PRIMARY KEY (session_id,object_id),
    FOREIGN KEY (session_id,object_id) REFERENCES objects(session_id,object_id)
);
CREATE TABLE door_credentials (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    PRIMARY KEY (session_id,object_id,instance_id),
    FOREIGN KEY (session_id,object_id) REFERENCES door_locks(session_id,object_id),
    FOREIGN KEY (session_id,instance_id) REFERENCES objects(session_id,object_id)
);
CREATE TRIGGER lock_insert_state BEFORE INSERT ON door_locks
WHEN NOT EXISTS (SELECT 1 FROM objects WHERE session_id=NEW.session_id
    AND object_id=NEW.object_id AND state IN ('open','closed') AND item_json IS NULL
    AND (NEW.locked=0 OR state='closed'))
BEGIN SELECT RAISE(ABORT,'Invalid door lock state'); END;
CREATE TRIGGER lock_update_state BEFORE UPDATE ON door_locks
WHEN NOT EXISTS (SELECT 1 FROM objects WHERE session_id=NEW.session_id
    AND object_id=NEW.object_id AND state IN ('open','closed') AND item_json IS NULL
    AND (NEW.locked=0 OR state='closed'))
BEGIN SELECT RAISE(ABORT,'Invalid door lock state'); END;
CREATE TRIGGER locked_door_switch BEFORE UPDATE OF state ON objects
WHEN NEW.state IS NOT 'closed' AND EXISTS (SELECT 1 FROM door_locks
    WHERE session_id=OLD.session_id AND object_id=OLD.object_id AND locked=1)
BEGIN SELECT RAISE(ABORT,'Locked door must remain closed'); END;
PRAGMA user_version = 25;
COMMIT;
