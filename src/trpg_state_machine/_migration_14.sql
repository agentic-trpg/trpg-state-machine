BEGIN IMMEDIATE;
-- A selected Result already atomically recorded every output handoff in SM-16.
UPDATE npc_observation_work SET status='completed',disposition='evaluation'
WHERE status='pending' AND EXISTS (
    SELECT 1 FROM npc_evaluation_observations e JOIN npc_evaluation_tasks t USING(evaluation_id)
    JOIN npc_evaluation_receipts r USING(evaluation_id)
    WHERE e.work_id=npc_observation_work.work_id AND t.status='completed'
);
CREATE TABLE npc_completion_cursors (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK(event_seq>=0 AND typeof(event_seq)='integer'),
    PRIMARY KEY(session_id,npc_id),
    FOREIGN KEY(session_id,npc_id) REFERENCES npc_states(session_id,npc_id)
);
INSERT INTO npc_completion_cursors
SELECT n.session_id,n.npc_id,coalesce((
    SELECT min(o.event_seq)-1 FROM npc_observation_work w
    JOIN event_observations o USING(observation_id)
    WHERE w.session_id=n.session_id AND w.npc_id=n.npc_id AND w.status='pending'
      AND o.event_seq<=coalesce(c.event_seq,0)
),coalesce(c.event_seq,0)) FROM npc_states n LEFT JOIN projection_cursors c
ON c.session_id=n.session_id AND c.consumer='perception';
CREATE TRIGGER npc_output_identity_immutable BEFORE UPDATE ON npc_output_handoffs
WHEN NEW.evaluation_id!=OLD.evaluation_id OR NEW.ordinal!=OLD.ordinal OR NEW.kind!=OLD.kind
  OR NEW.command_id IS NOT OLD.command_id OR NEW.payload_json!=OLD.payload_json
  OR (OLD.status!='pending' AND (NEW.status!=OLD.status OR NEW.error_code IS NOT OLD.error_code
      OR NEW.event_seq IS NOT OLD.event_seq))
BEGIN SELECT RAISE(ABORT,'Output identity and final delivery are immutable'); END;
CREATE TRIGGER npc_output_retained BEFORE DELETE ON npc_output_handoffs
BEGIN SELECT RAISE(ABORT,'Reliable output handoff is retained'); END;
PRAGMA user_version = 14;
COMMIT;
