BEGIN IMMEDIATE;
CREATE TABLE object_durability (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    current_hp INTEGER NOT NULL CHECK(current_hp>=0),
    PRIMARY KEY(session_id,object_id),
    FOREIGN KEY(session_id,object_id) REFERENCES objects(session_id,object_id)
);
CREATE TABLE world_result_adoptions (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    resolution_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY(session_id,resolution_id)
);
PRAGMA user_version = 26;
COMMIT;
