CREATE TABLE session_time_waits (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    wait_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    domain_wait_id TEXT NOT NULL,
    domain_version INTEGER NOT NULL CHECK(domain_version > 0),
    boundary_tick INTEGER NOT NULL CHECK(boundary_tick >= 0),
    origin_command_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting','resolved','cancelled','interrupted')),
    PRIMARY KEY(session_id,wait_id)
);
CREATE UNIQUE INDEX session_time_active_wait ON session_time_waits(session_id)
    WHERE status='waiting';
PRAGMA user_version = 38;
