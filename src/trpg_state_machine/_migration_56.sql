-- Paused fuel is a balance. Burning fuel is represented by its sole world-time deadline.
ALTER TABLE object_lights ADD COLUMN fuel_remaining_seconds INTEGER DEFAULT NULL
    CHECK (fuel_remaining_seconds IS NULL OR
           (enabled = 0 AND typeof(fuel_remaining_seconds) = 'integer'
            AND fuel_remaining_seconds BETWEEN 0 AND 2147483647));
ALTER TABLE object_lights ADD COLUMN fuel_expires_tick INTEGER DEFAULT NULL
    CHECK (fuel_expires_tick IS NULL OR
           (enabled = 1 AND fuel_remaining_seconds IS NULL
            AND typeof(fuel_expires_tick) = 'integer'
            AND fuel_expires_tick BETWEEN 1 AND 2147483647));
CREATE INDEX idx_object_lights_fuel_expiry ON object_lights(session_id,fuel_expires_tick,object_id)
    WHERE fuel_expires_tick IS NOT NULL;
PRAGMA user_version = 56;
