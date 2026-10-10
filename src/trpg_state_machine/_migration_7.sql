BEGIN IMMEDIATE;
CREATE TABLE rng_states_new (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    state_id TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    encoding TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    rng_version INTEGER NOT NULL DEFAULT 0 CHECK(rng_version >= 0),
    state_json TEXT,
    UNIQUE(session_id,state_id),
    CHECK ((algorithm='unselected' AND encoding='foundation-placeholder/1'
            AND position=0 AND rng_version=0 AND state_json IS NULL)
        OR (algorithm='synthetic-sha256-counter/1' AND encoding='synthetic-counter-json/1'
            AND state_json IS NOT NULL)
        OR (algorithm='stdlib-mt19937' AND encoding='stdlib-mt19937/1'
            AND position=0 AND state_json IS NOT NULL))
);
INSERT INTO rng_states_new SELECT * FROM rng_states;
DROP TABLE rng_states;
ALTER TABLE rng_states_new RENAME TO rng_states;
-- HP stays in characters; these rows contain the remaining Engine actor fields.
CREATE TABLE mechanical_actors (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    state_json TEXT NOT NULL CHECK(json_valid(state_json)),
    PRIMARY KEY(session_id,actor_id),
    FOREIGN KEY(session_id,actor_id) REFERENCES characters(session_id,actor_id)
);
CREATE TABLE mechanical_scenes (
    session_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    state_json TEXT NOT NULL CHECK(json_valid(state_json)),
    PRIMARY KEY(session_id,scene_id),
    FOREIGN KEY(session_id,scene_id) REFERENCES scene_instances(session_id,scene_id)
);
CREATE TABLE combats (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    combat_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    state_json TEXT NOT NULL CHECK(json_valid(state_json)),
    process_id TEXT NOT NULL,
    FOREIGN KEY(session_id,scene_id) REFERENCES mechanical_scenes(session_id,scene_id)
);
PRAGMA user_version = 7;
COMMIT;
