-- Only a fully committed one-step activity can become the actor's last long rest.
CREATE TABLE latest_long_rests (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    start_tick INTEGER NOT NULL CHECK(start_tick >= 0),
    completed_tick INTEGER NOT NULL CHECK(completed_tick > start_tick),
    PRIMARY KEY(session_id, actor_id),
    FOREIGN KEY(session_id, actor_id) REFERENCES characters(session_id, actor_id)
);
PRAGMA user_version = 55;
