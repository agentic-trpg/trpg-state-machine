-- Fresh-development Schema49: portable item scalar facts have typed columns.
-- The old JSON column becomes a structural marker required by legacy object CHECKs.
ALTER TABLE objects RENAME COLUMN item_json TO portable_marker;
ALTER TABLE objects ADD COLUMN item_slug TEXT CHECK(item_slug IS NULL OR length(item_slug) > 0);
ALTER TABLE objects ADD COLUMN item_charges INTEGER
    CHECK(item_charges IS NULL OR (typeof(item_charges)='integer' AND item_charges >= 0));
ALTER TABLE objects ADD COLUMN item_accessible INTEGER
    CHECK((portable_marker IS NULL AND item_slug IS NULL AND item_charges IS NULL AND item_accessible IS NULL)
       OR (portable_marker IS NOT NULL AND portable_marker = '{}' AND item_slug IS NOT NULL AND item_charges IS NOT NULL
           AND typeof(item_accessible)='integer' AND item_accessible IN (0,1)));
PRAGMA user_version = 49;
