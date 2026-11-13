-- Fresh databases complete Schema41 with item facts in objects only.
ALTER TABLE characters DROP COLUMN inventory_json;
ALTER TABLE mechanical_components DROP COLUMN inventory_json;
PRAGMA user_version = 41;
