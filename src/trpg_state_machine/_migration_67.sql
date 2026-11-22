-- Fresh development schema: retain verified actual spell level on target applications.
ALTER TABLE ongoing_effects ADD COLUMN cast_slot_level INTEGER
    CHECK(cast_slot_level IS NULL OR cast_slot_level BETWEEN 0 AND 9);
PRAGMA user_version = 67;
