BEGIN IMMEDIATE;
-- Authorization references only, never a second copy of OB or a new Work queue.
-- Old tasks gain no invented Recent grant; their original Work scope is preserved.
CREATE TABLE npc_evaluation_recent_scope (
    evaluation_id TEXT NOT NULL REFERENCES npc_evaluation_tasks(evaluation_id),
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    PRIMARY KEY(evaluation_id,observation_id),
    FOREIGN KEY(session_id,npc_id,observation_id)
        REFERENCES npc_inbox(session_id,observer_id,observation_id)
);
CREATE TRIGGER npc_evaluation_recent_owner BEFORE INSERT ON npc_evaluation_recent_scope
WHEN NOT EXISTS (SELECT 1 FROM npc_evaluation_tasks t WHERE t.evaluation_id=NEW.evaluation_id
    AND t.session_id=NEW.session_id AND t.npc_id=NEW.npc_id AND t.status='running')
BEGIN SELECT RAISE(ABORT,'Recent authorization requires own valid Task'); END;
CREATE TRIGGER npc_evaluation_recent_immutable BEFORE UPDATE ON npc_evaluation_recent_scope
BEGIN SELECT RAISE(ABORT,'Task Recent authorization is immutable'); END;
CREATE TRIGGER npc_evaluation_recent_retained BEFORE DELETE ON npc_evaluation_recent_scope
BEGIN SELECT RAISE(ABORT,'Task Recent authorization is retained'); END;
PRAGMA user_version = 18;
COMMIT;
