BEGIN IMMEDIATE;
CREATE TABLE object_mechanisms (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('armed','spent')),
    PRIMARY KEY(session_id,object_id),
    FOREIGN KEY(session_id,object_id) REFERENCES objects(session_id,object_id)
);
PRAGMA user_version = 31;
COMMIT;
