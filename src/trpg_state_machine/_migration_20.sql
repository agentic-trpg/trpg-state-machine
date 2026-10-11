BEGIN IMMEDIATE;
CREATE INDEX npc_perception_pending
    ON npc_perception_queue(session_id,npc_id,event_seq) WHERE complete=0;
-- A derived prefix is no longer persisted. Queue/proofs/barriers are retained.
DROP TABLE npc_perception_cursors;
PRAGMA user_version = 20;
COMMIT;
