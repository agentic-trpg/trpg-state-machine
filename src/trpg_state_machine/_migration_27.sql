BEGIN IMMEDIATE;
CREATE TABLE object_openables (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    jammed INTEGER NOT NULL CHECK(jammed IN (0,1)),
    barred INTEGER CHECK(barred IN (0,1)),
    PRIMARY KEY(session_id,object_id),
    FOREIGN KEY(session_id,object_id) REFERENCES objects(session_id,object_id)
);
PRAGMA user_version = 27;
COMMIT;
