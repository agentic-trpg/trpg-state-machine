PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
CREATE TABLE rng_states_new (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    state_id TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    encoding TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    rng_version INTEGER NOT NULL DEFAULT 0 CHECK(rng_version >= 0),
    state_json TEXT,
    UNIQUE(session_id, state_id),
    CHECK ((algorithm='unselected' AND encoding='foundation-placeholder/1'
            AND position=0 AND rng_version=0 AND state_json IS NULL)
        OR (algorithm='synthetic-sha256-counter/1' AND encoding='synthetic-counter-json/1'
            AND state_json IS NOT NULL))
);
INSERT INTO rng_states_new(session_id,state_id,algorithm,encoding,position)
SELECT session_id,state_id,algorithm,encoding,position FROM rng_states;
DROP TABLE rng_states;
ALTER TABLE rng_states_new RENAME TO rng_states;
ALTER TABLE characters ADD COLUMN effects_json TEXT NOT NULL DEFAULT '[]';
PRAGMA user_version = 4;
COMMIT;
PRAGMA foreign_keys = ON;
