-- Preserve actor deadline order and settle same-tick effects by effect-list ordinal.
DROP INDEX ongoing_effects_world_deadline;
CREATE INDEX ongoing_effects_world_deadline
    ON ongoing_effects(session_id,expires_tick,target_id,ordinal)
    WHERE expires_tick IS NOT NULL;
PRAGMA user_version = 63;
