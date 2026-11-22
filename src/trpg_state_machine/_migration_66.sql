-- Initial Staff of Healing attunement belongs to the exact item instance.
ALTER TABLE objects ADD COLUMN item_attuned_actor_id TEXT
    CHECK (item_attuned_actor_id IS NULL OR (
        item_slug IS NOT NULL AND item_slug = 'staff-of-healing'
        AND typeof(item_attuned_actor_id) = 'text' AND length(item_attuned_actor_id) > 0
    ));

PRAGMA user_version = 66;
