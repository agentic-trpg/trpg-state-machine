-- Fresh development schema: retain verified actual spell level on target applications.
ALTER TABLE ongoing_effects ADD COLUMN cast_slot_level INTEGER
    CHECK(cast_slot_level IS NULL OR cast_slot_level BETWEEN 0 AND 9);

-- These Effects indexes locate fixed origins for local sight and lighting.
CREATE INDEX spatial_light_obscuring_radius
    ON spatial_light_effects(session_id, fixed_scene_id, radius_ft DESC)
    WHERE fixed_cell IS NOT NULL
      AND json_extract(environment_json, '$.obscurement') != 'none';
CREATE INDEX spatial_light_obscuring_cell
    ON spatial_light_effects(session_id, fixed_scene_id, fixed_cell)
    WHERE fixed_cell IS NOT NULL
      AND json_extract(environment_json, '$.obscurement') != 'none';
CREATE INDEX spatial_light_illuminating_scene
    ON spatial_light_effects(session_id, fixed_scene_id, effect_id)
    WHERE fixed_cell IS NOT NULL
      AND json_extract(environment_json, '$.light') IS NOT NULL;

PRAGMA user_version = 67;
