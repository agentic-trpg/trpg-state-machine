BEGIN IMMEDIATE;
-- Local game-clock/1: abstract ticks, never wall-clock seconds or combat rounds.
ALTER TABLE world_states ADD COLUMN game_tick INTEGER NOT NULL DEFAULT 0
    CHECK(typeof(game_tick)='integer' AND game_tick BETWEEN 0 AND 2147483647);
CREATE TRIGGER game_clock_monotonic BEFORE UPDATE OF game_tick ON world_states
WHEN NEW.game_tick < OLD.game_tick
BEGIN SELECT RAISE(ABORT,'Game time cannot move backwards'); END;
-- Lifecycle of the existing NPC Runtime schedule, not another schedule authority.
CREATE TABLE npc_schedule_runs (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    schedule_id TEXT NOT NULL CHECK(length(schedule_id) BETWEEN 1 AND 128),
    status TEXT NOT NULL CHECK(status IN ('active','completed','interrupted','missed')),
    started_tick INTEGER CHECK(started_tick IS NULL OR
        (typeof(started_tick)='integer' AND started_tick>=0)),
    finished_tick INTEGER CHECK(finished_tick IS NULL OR
        (typeof(finished_tick)='integer' AND finished_tick>=0)),
    updated_tick INTEGER NOT NULL CHECK(typeof(updated_tick)='integer' AND updated_tick>=0),
    source_command_id TEXT NOT NULL,
    PRIMARY KEY(session_id,npc_id,schedule_id),
    FOREIGN KEY(session_id,npc_id) REFERENCES npc_states(session_id,npc_id),
    FOREIGN KEY(session_id,source_command_id) REFERENCES command_receipts(session_id,command_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK((status='active' AND started_tick IS NOT NULL AND finished_tick IS NULL)
       OR (status IN ('completed','interrupted') AND started_tick IS NOT NULL
           AND finished_tick IS NOT NULL AND finished_tick>=started_tick)
       OR (status='missed' AND started_tick IS NULL AND finished_tick IS NOT NULL)),
    CHECK(updated_tick=coalesce(finished_tick,started_tick))
);
CREATE UNIQUE INDEX npc_one_active_schedule ON npc_schedule_runs(session_id,npc_id)
WHERE status='active';
CREATE TRIGGER npc_schedule_run_retained BEFORE DELETE ON npc_schedule_runs
BEGIN SELECT RAISE(ABORT,'Schedule lifecycle is retained'); END;
CREATE TRIGGER npc_schedule_run_transition BEFORE UPDATE ON npc_schedule_runs
WHEN OLD.status!='active' OR NEW.status NOT IN ('completed','interrupted')
  OR NEW.session_id!=OLD.session_id OR NEW.npc_id!=OLD.npc_id
  OR NEW.schedule_id!=OLD.schedule_id OR NEW.started_tick IS NOT OLD.started_tick
  OR NEW.updated_tick<OLD.updated_tick
BEGIN SELECT RAISE(ABORT,'Invalid schedule lifecycle transition'); END;
PRAGMA user_version = 15;
COMMIT;
