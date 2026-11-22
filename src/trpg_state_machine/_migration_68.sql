-- Locate fixed Silence areas by radius and origin for cast-local membership.
CREATE INDEX spatial_silence_radius
    ON spatial_light_effects(session_id, fixed_scene_id, radius_ft DESC)
    WHERE fixed_cell IS NOT NULL
      AND json_extract(environment_json, '$.kind') = 'magical_silence';
CREATE INDEX spatial_silence_cell
    ON spatial_light_effects(session_id, fixed_scene_id, fixed_cell)
    WHERE fixed_cell IS NOT NULL
      AND json_extract(environment_json, '$.kind') = 'magical_silence';

PRAGMA user_version = 68;
