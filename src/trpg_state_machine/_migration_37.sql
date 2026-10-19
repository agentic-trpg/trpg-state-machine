CREATE TABLE session_time_runs (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    mode TEXT NOT NULL CHECK(mode IN ('paused','running')),
    rate_numerator INTEGER CHECK(rate_numerator > 0),
    rate_denominator INTEGER CHECK(rate_denominator > 0),
    generation INTEGER NOT NULL DEFAULT 0,
    remainder TEXT NOT NULL DEFAULT '0',
    CHECK((rate_numerator IS NULL) = (rate_denominator IS NULL))
);
CREATE TABLE session_time_progress (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    target_tick INTEGER NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE time_command_admissions (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    command_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    original_version INTEGER NOT NULL,
    execution_version INTEGER NOT NULL,
    target_tick INTEGER NOT NULL,
    ready INTEGER NOT NULL,
    start_event_seq INTEGER NOT NULL,
    source TEXT NOT NULL,
    target_fraction TEXT NOT NULL,
    PRIMARY KEY(session_id,command_id)
);
CREATE INDEX mechanism_time_boundary ON object_mechanisms(session_id,reset_at_tick)
    WHERE reset_at_tick IS NOT NULL;
PRAGMA user_version = 37;
