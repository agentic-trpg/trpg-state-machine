BEGIN IMMEDIATE;
CREATE TABLE navigation_activities (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    activity_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    target_scene_id TEXT NOT NULL,
    target_cell TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','blocked','completed','cancelled')),
    activity_version INTEGER NOT NULL CHECK(activity_version >= 1),
    cursor INTEGER NOT NULL CHECK(cursor >= 0),
    plan_json TEXT NOT NULL,
    last_result_json TEXT NOT NULL,
    source_command_id TEXT NOT NULL,
    last_command_id TEXT NOT NULL,
    controller_principal_id TEXT,
    PRIMARY KEY(session_id, activity_id),
    FOREIGN KEY(session_id, actor_id) REFERENCES actors(session_id, actor_id)
);
CREATE UNIQUE INDEX navigation_actor_active
    ON navigation_activities(session_id, actor_id) WHERE status='active';
PRAGMA user_version = 22;
COMMIT;
