BEGIN IMMEDIATE;
INSERT INTO event_outbox(session_id,event_seq,consumer)
SELECT session_id,event_seq,'perception' FROM event_records;
CREATE TABLE npc_access (
    session_id TEXT NOT NULL,
    observer_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    PRIMARY KEY(session_id,observer_id),
    FOREIGN KEY(session_id,observer_id) REFERENCES actors(session_id,actor_id)
);
CREATE TABLE perception_results (
    session_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    observer_id TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK(complete IN (0,1)),
    PRIMARY KEY(session_id,event_seq,observer_id),
    FOREIGN KEY(session_id,event_seq) REFERENCES event_records(session_id,event_seq),
    FOREIGN KEY(session_id,observer_id) REFERENCES actors(session_id,actor_id)
);
CREATE TABLE event_observations (
    session_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    observer_id TEXT NOT NULL,
    projection_version INTEGER NOT NULL CHECK(projection_version=1),
    observation_id TEXT NOT NULL UNIQUE,
    content_json TEXT NOT NULL,
    PRIMARY KEY(session_id,event_seq,observer_id,projection_version),
    FOREIGN KEY(session_id,event_seq,observer_id)
        REFERENCES perception_results(session_id,event_seq,observer_id)
);
CREATE TABLE npc_inbox (
    session_id TEXT NOT NULL,
    observer_id TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES event_observations(observation_id),
    PRIMARY KEY(session_id,observer_id,observation_id),
    FOREIGN KEY(session_id,observer_id) REFERENCES actors(session_id,actor_id)
);
PRAGMA user_version = 5;
COMMIT;
