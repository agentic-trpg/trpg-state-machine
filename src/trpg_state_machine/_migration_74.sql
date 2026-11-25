-- SM owns encounter withdrawal metadata independently of the rules snapshot.
ALTER TABLE combats ADD COLUMN region_json TEXT
    CHECK (region_json IS NULL OR (json_valid(region_json) AND json_type(region_json) = 'array'));
ALTER TABLE combats ADD COLUMN withdrawal_distance_ft INTEGER
    CHECK (withdrawal_distance_ft IS NULL OR
        (typeof(withdrawal_distance_ft) = 'integer' AND withdrawal_distance_ft > 0
        AND region_json IS NULL));
PRAGMA user_version = 74;
