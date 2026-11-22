-- Fixed Identify selections and private completion evidence belong to the casting Activity.
ALTER TABLE spell_cast_activities ADD COLUMN target_object_id TEXT
    CHECK(target_object_id IS NULL OR (spell_id = 'identify' AND length(target_object_id) BETWEEN 1 AND 256));
ALTER TABLE spell_cast_activities ADD COLUMN material_instance_id TEXT
    CHECK((target_object_id IS NULL) = (material_instance_id IS NULL) AND
          (material_instance_id IS NULL OR
              (length(material_instance_id) BETWEEN 1 AND 256 AND material_instance_id <> target_object_id)));
ALTER TABLE spell_cast_activities ADD COLUMN identification_json TEXT
    CHECK(identification_json IS NULL OR
          (target_object_id IS NOT NULL AND status IN ('waiting', 'completed') AND
              json_valid(identification_json)));
PRAGMA user_version = 65;
