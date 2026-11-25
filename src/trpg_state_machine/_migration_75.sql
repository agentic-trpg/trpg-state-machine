-- The trusted host's condition identity is fixed with the encounter, not its result.
ALTER TABLE combats ADD COLUMN end_condition_id TEXT
    CHECK (end_condition_id IS NULL OR (length(end_condition_id) BETWEEN 1 AND 256));
PRAGMA user_version = 75;
