-- World-time effect expiry is an indexed deadline for RE's terminal removal.
ALTER TABLE ongoing_effects ADD COLUMN expires_tick INTEGER
    CHECK(expires_tick IS NULL OR (typeof(expires_tick)='integer' AND expires_tick>=0));
CREATE INDEX ongoing_effects_world_deadline
    ON ongoing_effects(session_id,expires_tick,target_id,effect_id,origin)
    WHERE expires_tick IS NOT NULL;
PRAGMA user_version = 54;
