BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    adventure_id TEXT NOT NULL,
    package_version TEXT NOT NULL,
    package_digest TEXT NOT NULL,
    ruleset_id TEXT NOT NULL,
    data_revision TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    pc_template_id TEXT NOT NULL,
    pc_actor_id TEXT NOT NULL,
    current_scene_id TEXT NOT NULL,
    rng_ref TEXT NOT NULL,
    world_version INTEGER NOT NULL CHECK(world_version >= 0),
    event_seq INTEGER NOT NULL CHECK(event_seq >= 0),
    FOREIGN KEY(session_id, current_scene_id) REFERENCES scene_instances(session_id, scene_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id, pc_actor_id) REFERENCES actors(session_id, actor_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id, rng_ref) REFERENCES rng_states(session_id, state_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS world_states (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    flags_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scene_instances (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    scene_id TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    visited_count INTEGER NOT NULL CHECK(visited_count > 0),
    PRIMARY KEY(session_id, scene_id)
);
CREATE TABLE IF NOT EXISTS objects (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('closed', 'open')),
    PRIMARY KEY(session_id, object_id),
    FOREIGN KEY(session_id, scene_id) REFERENCES scene_instances(session_id, scene_id)
);
CREATE TABLE IF NOT EXISTS actors (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    actor_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('pc', 'npc')),
    current_scene_id TEXT NOT NULL,
    PRIMARY KEY(session_id, actor_id),
    FOREIGN KEY(session_id, current_scene_id) REFERENCES scene_instances(session_id, scene_id)
);
CREATE TABLE IF NOT EXISTS characters (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    current_hp INTEGER NOT NULL CHECK(current_hp >= 0 AND current_hp <= max_hp),
    max_hp INTEGER NOT NULL CHECK(max_hp > 0),
    resources_json TEXT NOT NULL,
    inventory_json TEXT NOT NULL,
    PRIMARY KEY(session_id, actor_id),
    FOREIGN KEY(session_id, actor_id) REFERENCES actors(session_id, actor_id)
);
CREATE TABLE IF NOT EXISTS rng_states (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    state_id TEXT NOT NULL,
    algorithm TEXT NOT NULL CHECK(algorithm = 'unselected'),
    encoding TEXT NOT NULL CHECK(encoding = 'foundation-placeholder/1'),
    position INTEGER NOT NULL CHECK(position = 0),
    UNIQUE(session_id, state_id)
);
-- Internal lifecycle audit only; not the full public WorldEvent/Perception protocol.
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_seq INTEGER NOT NULL CHECK(event_seq > 0),
    event_type TEXT NOT NULL,
    world_version INTEGER NOT NULL CHECK(world_version >= 0),
    payload_json TEXT NOT NULL,
    PRIMARY KEY(session_id, event_seq)
);
CREATE TABLE IF NOT EXISTS creation_commands (
    principal_id TEXT NOT NULL,
    command_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(session_id),
    result_json TEXT NOT NULL,
    PRIMARY KEY(principal_id, command_id)
);
PRAGMA user_version = 1;
COMMIT;
