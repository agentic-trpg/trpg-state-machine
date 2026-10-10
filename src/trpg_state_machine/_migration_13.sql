BEGIN IMMEDIATE;
CREATE TABLE npc_modes (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('interactive','inactive')),
    PRIMARY KEY(session_id,npc_id),
    FOREIGN KEY(session_id,npc_id) REFERENCES npc_states(session_id,npc_id)
);
INSERT INTO npc_modes
SELECT n.session_id,n.npc_id,coalesce((
    SELECT CASE WHEN t.mode='interactive' THEN 'interactive' ELSE 'inactive' END
    FROM npc_evaluation_tasks t WHERE t.session_id=n.session_id AND t.npc_id=n.npc_id
    ORDER BY (t.status='running') DESC,t.rowid DESC LIMIT 1
),'inactive') FROM npc_states n;
CREATE TABLE npc_mode_requests (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
    requested_mode TEXT NOT NULL CHECK(requested_mode IN ('interactive','inactive')),
    evaluation_id TEXT REFERENCES npc_evaluation_tasks(evaluation_id),
    PRIMARY KEY(session_id,npc_id,principal_id,request_id),
    FOREIGN KEY(session_id,npc_id) REFERENCES npc_modes(session_id,npc_id)
);
CREATE TABLE npc_gate_decisions (
    work_id TEXT PRIMARY KEY REFERENCES npc_observation_work(work_id),
    principal_id TEXT NOT NULL,
    policy_version TEXT NOT NULL CHECK(policy_version='npc-gate/1'),
    decision TEXT NOT NULL CHECK(decision IN ('safe_retention','no_op'))
);
CREATE TABLE npc_routing_requests (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
    classification TEXT NOT NULL,
    response_json TEXT NOT NULL CHECK(json_valid(response_json) AND length(response_json)<=131072),
    PRIMARY KEY(session_id,npc_id,principal_id,request_id),
    FOREIGN KEY(session_id,npc_id) REFERENCES npc_states(session_id,npc_id)
);
PRAGMA user_version = 13;
COMMIT;
