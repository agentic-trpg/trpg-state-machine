BEGIN IMMEDIATE;
-- XP has one writable owner, alongside core HP and carried equipment.
-- Historical sessions acquire zero only for this previously unrepresented field;
-- their ruleset, package, mechanical components, RNG and receipts are untouched.
ALTER TABLE characters ADD COLUMN xp INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0);
-- NULL denotes the historical /1-only runtime; it does not upgrade any binding.
ALTER TABLE sessions ADD COLUMN mechanical_schema_version TEXT
    CHECK(mechanical_schema_version IN ('engine-snapshot/1','engine-snapshot/2'));
PRAGMA user_version = 9;
COMMIT;
