-- Bound passive detection reads to exact fixed spell origins in the observer's range.
CREATE INDEX spatial_magic_fixed_cell
    ON spatial_light_effects(session_id, fixed_scene_id, fixed_cell)
    WHERE fixed_cell IS NOT NULL;

PRAGMA user_version = 69;
