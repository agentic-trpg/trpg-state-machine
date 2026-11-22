-- An ongoing spell keeps its original cast owner independently of its display origin.
ALTER TABLE ongoing_effects ADD COLUMN cast_by TEXT;
ALTER TABLE ongoing_effects ADD COLUMN cast_spell_id TEXT;
ALTER TABLE ongoing_effects ADD COLUMN cast_command_id TEXT CHECK (
    (cast_by IS NULL AND cast_spell_id IS NULL AND cast_command_id IS NULL)
    OR (cast_by IS NOT NULL AND length(cast_by) > 0
        AND cast_spell_id IS NOT NULL AND length(cast_spell_id) > 0
        AND cast_command_id IS NOT NULL AND length(cast_command_id) > 0
        AND expires_tick IS NOT NULL)
);
PRAGMA user_version = 64;
