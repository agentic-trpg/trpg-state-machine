BEGIN IMMEDIATE;
-- Local ob-retention/1: first durable Work adoption, not historical event time.
-- NULL/NULL deliberately preserves unknown timing of every pre-upgrade Work.
ALTER TABLE npc_observation_work ADD COLUMN accepted_game_tick INTEGER
    CHECK(accepted_game_tick IS NULL OR (typeof(accepted_game_tick)='integer' AND accepted_game_tick>=0));
ALTER TABLE npc_observation_work ADD COLUMN expires_game_tick INTEGER
    CHECK((accepted_game_tick IS NULL AND expires_game_tick IS NULL) OR
          (accepted_game_tick IS NOT NULL AND typeof(expires_game_tick)='integer'
           AND expires_game_tick>accepted_game_tick));
CREATE TRIGGER npc_observation_adoption AFTER INSERT ON npc_observation_work
BEGIN
    UPDATE npc_observation_work
    SET accepted_game_tick=(SELECT game_tick FROM world_states WHERE session_id=NEW.session_id),
        expires_game_tick=(SELECT game_tick+100 FROM world_states WHERE session_id=NEW.session_id)
    WHERE work_id=NEW.work_id;
END;
CREATE TRIGGER npc_observation_timing_immutable BEFORE UPDATE ON npc_observation_work
WHEN OLD.accepted_game_tick IS NOT NULL AND
    (NEW.accepted_game_tick IS NOT OLD.accepted_game_tick OR
     NEW.expires_game_tick IS NOT OLD.expires_game_tick)
BEGIN SELECT RAISE(ABORT,'Observation lifetime cannot be reset'); END;
-- Logical expiry only. No evidence, cursor, dedupe key or Work is garbage-collected.
CREATE TRIGGER npc_observation_work_retained BEFORE DELETE ON npc_observation_work
BEGIN SELECT RAISE(ABORT,'Observation Work is retained'); END;
CREATE TRIGGER npc_observation_evidence_retained BEFORE DELETE ON event_observations
BEGIN SELECT RAISE(ABORT,'Observation evidence is retained'); END;
CREATE TRIGGER npc_observation_inbox_retained BEFORE DELETE ON npc_inbox
BEGIN SELECT RAISE(ABORT,'Observation identity is retained'); END;
PRAGMA user_version = 16;
COMMIT;
