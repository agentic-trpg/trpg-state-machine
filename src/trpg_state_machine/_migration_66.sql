-- Exact-item attunement belongs to World; later current-schema capabilities
-- may establish additional bonds after a verified focused rest.
ALTER TABLE objects ADD COLUMN item_attuned_actor_id TEXT
    CHECK (item_attuned_actor_id IS NULL OR (
        item_slug IS NOT NULL
        AND typeof(item_attuned_actor_id) = 'text' AND length(item_attuned_actor_id) > 0
    ));

PRAGMA user_version = 66;
