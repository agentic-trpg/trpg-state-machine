-- Scene environment is World-owned; no FK to lazily initialized scene_instances.
CREATE TABLE IF NOT EXISTS scene_environments (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    scene_id TEXT NOT NULL,
    facts_json TEXT NOT NULL CHECK(json_valid(facts_json)),
    PRIMARY KEY(session_id,scene_id)
);
PRAGMA user_version = 35;
