BEGIN IMMEDIATE;
-- Work references the existing Inbox; it is not a second Observation store.
CREATE TABLE npc_observation_work (
    work_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed')),
    disposition TEXT CHECK(disposition IN ('evaluation','safe_retention','no_op')),
    PRIMARY KEY(session_id,npc_id,observation_id),
    FOREIGN KEY(session_id,npc_id,observation_id)
        REFERENCES npc_inbox(session_id,observer_id,observation_id),
    CHECK((status='pending' AND disposition IS NULL) OR
          (status='completed' AND disposition IS NOT NULL))
);
INSERT INTO npc_observation_work(work_id,session_id,npc_id,observation_id)
SELECT 'work.' || observation_id,session_id,observer_id,observation_id FROM npc_inbox;
-- Recovered only from saved positive judgments and committed Signal identities.
CREATE TABLE observation_signal_sources (
    observation_id TEXT NOT NULL REFERENCES event_observations(observation_id),
    source_event_id TEXT NOT NULL REFERENCES event_records(event_id),
    signal_id TEXT NOT NULL,
    PRIMARY KEY(observation_id,signal_id)
);
INSERT INTO observation_signal_sources
SELECT o.observation_id,r.event_id,json_extract(s.value,'$.signal_id')
FROM event_observations o JOIN event_records r USING(session_id,event_seq)
JOIN perception_results p USING(session_id,event_seq,observer_id),json_each(r.signals_json) s
WHERE p.complete=1 AND EXISTS (
    SELECT 1 FROM json_each(p.decisions_json) d
    WHERE d.key=json_extract(s.value,'$.signal_id') AND d.type='true'
);
CREATE TABLE npc_evaluation_tasks (
    evaluation_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK(schema_version='npc-evaluation/1'),
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
    mode TEXT NOT NULL CHECK(mode IN ('interactive','background')),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 4096),
    expected_npc_state_version INTEGER NOT NULL CHECK(expected_npc_state_version>=1),
    player_input TEXT CHECK(player_input IS NULL OR length(player_input) BETWEEN 1 AND 4096),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running','completed','cancelled','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    attempt_limit INTEGER NOT NULL DEFAULT 3 CHECK(attempt_limit BETWEEN 1 AND 8),
    last_error TEXT,
    FOREIGN KEY(session_id,npc_id) REFERENCES npc_states(session_id,npc_id),
    UNIQUE(session_id,npc_id,principal_id,request_id)
);
CREATE UNIQUE INDEX npc_single_evaluation ON npc_evaluation_tasks(session_id,npc_id)
WHERE status='running';
CREATE TABLE npc_evaluation_observations (
    evaluation_id TEXT NOT NULL REFERENCES npc_evaluation_tasks(evaluation_id),
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 99),
    work_id TEXT NOT NULL REFERENCES npc_observation_work(work_id),
    PRIMARY KEY(evaluation_id,ordinal),
    UNIQUE(evaluation_id,work_id)
);
PRAGMA user_version = 11;
COMMIT;
