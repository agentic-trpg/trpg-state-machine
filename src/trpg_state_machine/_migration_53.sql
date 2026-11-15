-- Fresh-development Schema53: one ordered spell row owns its material binding.
DROP TABLE character_spell_order;
DROP TABLE character_spell_materials;
ALTER TABLE character_spells ADD COLUMN material_instance_id TEXT
    CHECK(material_instance_id IS NULL OR length(material_instance_id) > 0);
ALTER TABLE character_spells ADD COLUMN material_available INTEGER
    CHECK(material_available IS NULL OR material_available IN (0, 1));
CREATE INDEX character_spells_material_owner
    ON character_spells(session_id,actor_id,material_instance_id);
CREATE INDEX character_spells_material_instance
    ON character_spells(session_id,material_instance_id)
    WHERE material_instance_id IS NOT NULL;
CREATE TRIGGER character_spells_material_pair_insert
BEFORE INSERT ON character_spells
WHEN (NEW.material_instance_id IS NULL) <> (NEW.material_available IS NULL)
BEGIN SELECT RAISE(ABORT,'Incomplete spell material binding'); END;
CREATE TRIGGER character_spells_material_pair_update
BEFORE UPDATE OF material_instance_id,material_available ON character_spells
WHEN (NEW.material_instance_id IS NULL) <> (NEW.material_available IS NULL)
BEGIN SELECT RAISE(ABORT,'Incomplete spell material binding'); END;
ALTER TABLE sessions RENAME COLUMN mechanical_schema_version TO prior_mechanical_schema_version;
ALTER TABLE sessions ADD COLUMN mechanical_schema_version TEXT
    CHECK(mechanical_schema_version IN ('engine-snapshot/1','engine-snapshot/2','engine-snapshot/17','engine-snapshot/18','engine-snapshot/19','engine-snapshot/20'));
UPDATE sessions SET mechanical_schema_version = prior_mechanical_schema_version;
ALTER TABLE sessions DROP COLUMN prior_mechanical_schema_version;
ALTER TABLE mechanical_components RENAME COLUMN schema_version TO prior_schema_version;
ALTER TABLE mechanical_components ADD COLUMN schema_version TEXT NOT NULL DEFAULT 'engine-snapshot/17'
    CHECK(schema_version IN ('engine-snapshot/17','engine-snapshot/18','engine-snapshot/19','engine-snapshot/20'));
UPDATE mechanical_components SET schema_version = prior_schema_version;
ALTER TABLE mechanical_components DROP COLUMN prior_schema_version;
PRAGMA user_version = 53;
