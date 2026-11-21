-- The bound recovery profile and its pending dawn belong to the item instance.
ALTER TABLE objects ADD COLUMN item_recharge_capacity INTEGER
    CHECK (item_recharge_capacity IS NULL OR (
        typeof(item_recharge_capacity) = 'integer' AND item_recharge_capacity > 0
        AND item_slug IS NOT NULL AND item_charges <= item_recharge_capacity
    ));
ALTER TABLE objects ADD COLUMN item_recharge_due_tick INTEGER
    CHECK (
        (item_recharge_capacity IS NULL AND item_recharge_due_tick IS NULL)
        OR (item_recharge_capacity IS NOT NULL AND (
            (item_charges = item_recharge_capacity AND item_recharge_due_tick IS NULL)
            OR (item_charges < item_recharge_capacity
                AND typeof(item_recharge_due_tick) = 'integer' AND item_recharge_due_tick >= 0)
        ))
    );
CREATE INDEX idx_objects_item_recharge_due
    ON objects(session_id, item_recharge_due_tick, object_id)
    WHERE item_recharge_due_tick IS NOT NULL;

PRAGMA user_version = 61;
