BEGIN IMMEDIATE;
CREATE TABLE object_lights (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    mode TEXT NOT NULL,
    PRIMARY KEY(session_id,object_id),
    FOREIGN KEY(session_id,object_id) REFERENCES objects(session_id,object_id)
);
PRAGMA user_version = 30;
COMMIT;
