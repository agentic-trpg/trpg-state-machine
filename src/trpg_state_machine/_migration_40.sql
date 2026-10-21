-- Derived lookup data only; all maintenance participates in the owning transaction.
CREATE TABLE object_spatial_cells (
    session_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    cell TEXT NOT NULL,
    blocks INTEGER NOT NULL CHECK(blocks IN (0,1)),
    PRIMARY KEY(session_id,scene_id,object_id,cell),
    FOREIGN KEY(session_id,object_id) REFERENCES objects(session_id,object_id) ON DELETE CASCADE
);
CREATE INDEX spatial_cell_lookup ON object_spatial_cells(session_id,scene_id,cell,blocks,object_id);
CREATE INDEX spatial_object_lookup ON object_spatial_cells(session_id,object_id);
CREATE INDEX actors_cell_lookup ON actors(session_id,current_scene_id,cell,actor_id);
CREATE TABLE scene_integrity (
    session_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK(complete IN (0,1)),
    PRIMARY KEY(session_id,scene_id),
    FOREIGN KEY(session_id,scene_id) REFERENCES scene_instances(session_id,scene_id) ON DELETE CASCADE
);
CREATE TABLE scene_integrity_dependencies (
    session_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    PRIMARY KEY(session_id,scene_id,domain,instance_id)
);
CREATE INDEX scene_dependency_lookup ON scene_integrity_dependencies(session_id,domain,instance_id,scene_id);
INSERT INTO object_spatial_cells
SELECT o.session_id,o.scene_id,o.object_id,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]')
FROM objects o LEFT JOIN instance_configs i ON i.session_id=o.session_id AND i.domain='object' AND i.instance_id=o.object_id
LEFT JOIN object_durability d ON d.session_id=o.session_id AND d.object_id=o.object_id,
json_each(runtime_spatial_cells(i.config_json,o.cell,o.orientation,o.state,d.current_hp)) c
WHERE o.scene_id IS NOT NULL AND o.cell IS NOT NULL;
CREATE TRIGGER spatial_index_objects_insert AFTER INSERT ON objects
BEGIN
DELETE FROM object_spatial_cells WHERE session_id=NEW.session_id AND object_id=NEW.object_id;
INSERT INTO object_spatial_cells
SELECT o.session_id,o.scene_id,o.object_id,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]')
FROM objects o LEFT JOIN instance_configs i ON i.session_id=o.session_id AND i.domain='object' AND i.instance_id=o.object_id
LEFT JOIN object_durability d ON d.session_id=o.session_id AND d.object_id=o.object_id,
json_each(runtime_spatial_cells(i.config_json,o.cell,o.orientation,o.state,d.current_hp)) c
WHERE o.scene_id IS NOT NULL AND o.cell IS NOT NULL AND o.session_id=NEW.session_id AND o.object_id=NEW.object_id;
END;
CREATE TRIGGER spatial_index_objects_update AFTER UPDATE OF scene_id,cell,orientation,state ON objects
BEGIN
DELETE FROM object_spatial_cells WHERE session_id=NEW.session_id AND object_id=NEW.object_id;
INSERT INTO object_spatial_cells
SELECT o.session_id,o.scene_id,o.object_id,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]')
FROM objects o LEFT JOIN instance_configs i ON i.session_id=o.session_id AND i.domain='object' AND i.instance_id=o.object_id
LEFT JOIN object_durability d ON d.session_id=o.session_id AND d.object_id=o.object_id,
json_each(runtime_spatial_cells(i.config_json,o.cell,o.orientation,o.state,d.current_hp)) c
WHERE o.scene_id IS NOT NULL AND o.cell IS NOT NULL AND o.session_id=NEW.session_id AND o.object_id=NEW.object_id;
END;
CREATE TRIGGER spatial_index_instance_configs_insert AFTER INSERT ON instance_configs WHEN NEW.domain='object'
BEGIN
DELETE FROM object_spatial_cells WHERE session_id=NEW.session_id AND object_id=NEW.instance_id;
INSERT INTO object_spatial_cells
SELECT o.session_id,o.scene_id,o.object_id,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]')
FROM objects o LEFT JOIN instance_configs i ON i.session_id=o.session_id AND i.domain='object' AND i.instance_id=o.object_id
LEFT JOIN object_durability d ON d.session_id=o.session_id AND d.object_id=o.object_id,
json_each(runtime_spatial_cells(i.config_json,o.cell,o.orientation,o.state,d.current_hp)) c
WHERE o.scene_id IS NOT NULL AND o.cell IS NOT NULL AND o.session_id=NEW.session_id AND o.object_id=NEW.instance_id;
END;
CREATE TRIGGER spatial_index_instance_configs_update AFTER UPDATE OF config_json ON instance_configs WHEN NEW.domain='object'
BEGIN
DELETE FROM object_spatial_cells WHERE session_id=NEW.session_id AND object_id=NEW.instance_id;
INSERT INTO object_spatial_cells
SELECT o.session_id,o.scene_id,o.object_id,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]')
FROM objects o LEFT JOIN instance_configs i ON i.session_id=o.session_id AND i.domain='object' AND i.instance_id=o.object_id
LEFT JOIN object_durability d ON d.session_id=o.session_id AND d.object_id=o.object_id,
json_each(runtime_spatial_cells(i.config_json,o.cell,o.orientation,o.state,d.current_hp)) c
WHERE o.scene_id IS NOT NULL AND o.cell IS NOT NULL AND o.session_id=NEW.session_id AND o.object_id=NEW.instance_id;
END;
CREATE TRIGGER spatial_index_object_durability_insert AFTER INSERT ON object_durability
BEGIN
DELETE FROM object_spatial_cells WHERE session_id=NEW.session_id AND object_id=NEW.object_id;
INSERT INTO object_spatial_cells
SELECT o.session_id,o.scene_id,o.object_id,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]')
FROM objects o LEFT JOIN instance_configs i ON i.session_id=o.session_id AND i.domain='object' AND i.instance_id=o.object_id
LEFT JOIN object_durability d ON d.session_id=o.session_id AND d.object_id=o.object_id,
json_each(runtime_spatial_cells(i.config_json,o.cell,o.orientation,o.state,d.current_hp)) c
WHERE o.scene_id IS NOT NULL AND o.cell IS NOT NULL AND o.session_id=NEW.session_id AND o.object_id=NEW.object_id;
END;
CREATE TRIGGER spatial_index_object_durability_update AFTER UPDATE OF current_hp ON object_durability
BEGIN
DELETE FROM object_spatial_cells WHERE session_id=NEW.session_id AND object_id=NEW.object_id;
INSERT INTO object_spatial_cells
SELECT o.session_id,o.scene_id,o.object_id,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]')
FROM objects o LEFT JOIN instance_configs i ON i.session_id=o.session_id AND i.domain='object' AND i.instance_id=o.object_id
LEFT JOIN object_durability d ON d.session_id=o.session_id AND d.object_id=o.object_id,
json_each(runtime_spatial_cells(i.config_json,o.cell,o.orientation,o.state,d.current_hp)) c
WHERE o.scene_id IS NOT NULL AND o.cell IS NOT NULL AND o.session_id=NEW.session_id AND o.object_id=NEW.object_id;
END;
CREATE TRIGGER spatial_index_object_durability_delete AFTER DELETE ON object_durability
BEGIN
DELETE FROM object_spatial_cells WHERE session_id=OLD.session_id AND object_id=OLD.object_id;
INSERT INTO object_spatial_cells
SELECT o.session_id,o.scene_id,o.object_id,json_extract(c.value,'$[0]'),json_extract(c.value,'$[1]')
FROM objects o LEFT JOIN instance_configs i ON i.session_id=o.session_id AND i.domain='object' AND i.instance_id=o.object_id
LEFT JOIN object_durability d ON d.session_id=o.session_id AND d.object_id=o.object_id,
json_each(runtime_spatial_cells(i.config_json,o.cell,o.orientation,o.state,d.current_hp)) c
WHERE o.scene_id IS NOT NULL AND o.cell IS NOT NULL AND o.session_id=OLD.session_id AND o.object_id=OLD.object_id;
END;
CREATE TRIGGER integrity_objects_delete AFTER DELETE ON objects
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='objects' AND instance_id=OLD.object_id); END;
CREATE TRIGGER integrity_actors_delete AFTER DELETE ON actors
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='actors' AND instance_id=OLD.actor_id); END;
CREATE TRIGGER integrity_object_openables_delete AFTER DELETE ON object_openables
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='object_openables' AND instance_id=OLD.object_id); END;
CREATE TRIGGER integrity_object_lights_delete AFTER DELETE ON object_lights
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='object_lights' AND instance_id=OLD.object_id); END;
CREATE TRIGGER integrity_object_mechanisms_delete AFTER DELETE ON object_mechanisms
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='object_mechanisms' AND instance_id=OLD.object_id); END;
CREATE TRIGGER integrity_object_durability_delete AFTER DELETE ON object_durability
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='object_durability' AND instance_id=OLD.object_id); END;
CREATE TRIGGER integrity_door_locks_delete AFTER DELETE ON door_locks
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='door_locks' AND instance_id=OLD.lock_id); END;
CREATE TRIGGER integrity_mechanical_scenes_delete AFTER DELETE ON mechanical_scenes
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='mechanical_scenes' AND instance_id=OLD.scene_id); END;
CREATE TRIGGER integrity_config_delete AFTER DELETE ON instance_configs
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='config:'||OLD.domain AND instance_id=OLD.instance_id); END;
CREATE TRIGGER integrity_config_update AFTER UPDATE OF config_json ON instance_configs
BEGIN UPDATE scene_integrity SET complete=0 WHERE session_id=OLD.session_id AND scene_id IN
(SELECT scene_id FROM scene_integrity_dependencies WHERE session_id=OLD.session_id AND domain='config:'||OLD.domain AND instance_id=OLD.instance_id); END;
PRAGMA user_version = 40;
