BEGIN IMMEDIATE;
-- Local npc-memory-gate/2: periodic=20, latest scheduling start=60, margin=10.
-- 60+10 < the local 100-tick lifetime. Origin never resets on batching/retry.
ALTER TABLE npc_observation_work ADD COLUMN gate_classification TEXT
    CHECK(gate_classification IN ('urgent','deferred','irrelevant','uncertain'));
ALTER TABLE npc_observation_work ADD COLUMN eligible_game_tick INTEGER
    CHECK(eligible_game_tick IS NULL OR (typeof(eligible_game_tick)='integer' AND eligible_game_tick>=0));
ALTER TABLE npc_observation_work ADD COLUMN latest_start_game_tick INTEGER
    CHECK(latest_start_game_tick IS NULL OR
          (typeof(latest_start_game_tick)='integer' AND accepted_game_tick IS NOT NULL
           AND eligible_game_tick<=latest_start_game_tick
           AND latest_start_game_tick+10<expires_game_tick));
UPDATE npc_observation_work SET eligible_game_tick=accepted_game_tick+20,
    latest_start_game_tick=accepted_game_tick+60 WHERE accepted_game_tick IS NOT NULL;
CREATE TRIGGER npc_observation_deadlines AFTER UPDATE OF accepted_game_tick ON npc_observation_work
WHEN OLD.accepted_game_tick IS NULL AND NEW.accepted_game_tick IS NOT NULL
BEGIN
    UPDATE npc_observation_work SET eligible_game_tick=NEW.accepted_game_tick+20,
        latest_start_game_tick=NEW.accepted_game_tick+60 WHERE work_id=NEW.work_id;
END;
CREATE TRIGGER npc_observation_deadline_not_reset BEFORE UPDATE ON npc_observation_work
WHEN OLD.latest_start_game_tick IS NOT NULL AND
    (NEW.latest_start_game_tick IS NOT OLD.latest_start_game_tick
     OR NEW.eligible_game_tick IS NULL OR NEW.eligible_game_tick>OLD.eligible_game_tick)
BEGIN SELECT RAISE(ABORT,'Observation waiting deadline cannot be reset'); END;
PRAGMA user_version = 17;
COMMIT;
