-- Legacy inventories have been checked and any unrepresented owned units
-- promoted to objects by inventory_migration in the same opening transaction.
ALTER TABLE characters DROP COLUMN inventory_json;
ALTER TABLE mechanical_components DROP COLUMN inventory_json;
PRAGMA user_version = 41;
