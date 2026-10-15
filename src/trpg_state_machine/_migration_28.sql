BEGIN IMMEDIATE;
DROP TRIGGER lock_insert_state;
DROP TRIGGER lock_update_state;
DROP TRIGGER locked_door_switch;
CREATE TABLE door_locks_w4 (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    locked INTEGER NOT NULL CHECK(locked IN (0,1)),
    lock_id TEXT NOT NULL,
    PRIMARY KEY(session_id,lock_id),
    UNIQUE(session_id,object_id,lock_id),
    FOREIGN KEY(session_id,object_id) REFERENCES objects(session_id,object_id)
);
INSERT INTO door_locks_w4 SELECT session_id,object_id,locked,'lock.' || object_id FROM door_locks;
CREATE TABLE door_credentials_w4 (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    lock_id TEXT NOT NULL,
    PRIMARY KEY(session_id,lock_id,instance_id),
    FOREIGN KEY(session_id,object_id,lock_id) REFERENCES door_locks_w4(session_id,object_id,lock_id),
    FOREIGN KEY(session_id,instance_id) REFERENCES objects(session_id,object_id)
);
INSERT INTO door_credentials_w4 SELECT session_id,object_id,instance_id,'lock.' || object_id
    FROM door_credentials;
DROP TABLE door_credentials;
DROP TABLE door_locks;
ALTER TABLE door_locks_w4 RENAME TO door_locks;
ALTER TABLE door_credentials_w4 RENAME TO door_credentials;
CREATE TRIGGER lock_insert_state BEFORE INSERT ON door_locks
WHEN NOT EXISTS (SELECT 1 FROM objects WHERE session_id=NEW.session_id
    AND object_id=NEW.object_id AND state IN ('open','closed')
    AND (NEW.locked=0 OR state='closed'))
BEGIN SELECT RAISE(ABORT,'Invalid object lock state'); END;
CREATE TRIGGER lock_update_state BEFORE UPDATE ON door_locks
WHEN NOT EXISTS (SELECT 1 FROM objects WHERE session_id=NEW.session_id
    AND object_id=NEW.object_id AND state IN ('open','closed')
    AND (NEW.locked=0 OR state='closed'))
BEGIN SELECT RAISE(ABORT,'Invalid object lock state'); END;
CREATE TRIGGER locked_door_switch BEFORE UPDATE OF state ON objects
WHEN NEW.state IS NOT 'closed' AND EXISTS (SELECT 1 FROM door_locks
    WHERE session_id=OLD.session_id AND object_id=OLD.object_id AND locked=1)
BEGIN SELECT RAISE(ABORT,'Locked object must remain closed'); END;
PRAGMA user_version = 28;
COMMIT;
