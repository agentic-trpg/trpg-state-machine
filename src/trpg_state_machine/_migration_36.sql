-- Two authoritative input revisions; no cell results or third cache revision.
CREATE TABLE scene_environment_versions (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    scene_id TEXT NOT NULL,
    source_version INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(source_version)='integer' AND source_version>=0),
    protection_version INTEGER NOT NULL DEFAULT 0
        CHECK(typeof(protection_version)='integer' AND protection_version>=0),
    PRIMARY KEY(session_id,scene_id)
);
CREATE TABLE environment_provider_bindings (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    scene_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('effects','protection')),
    descriptor TEXT NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(manifest_json)),
    PRIMARY KEY(session_id,scene_id,role)
);
INSERT INTO scene_environment_versions(session_id,scene_id)
SELECT session_id,scene_id FROM scene_instances
UNION SELECT session_id,scene_id FROM scene_environments
UNION SELECT session_id,scene_id FROM mechanical_scenes;

CREATE TRIGGER env_facts_scope_immutable BEFORE UPDATE OF session_id,scene_id ON scene_environments
WHEN OLD.session_id IS NOT NEW.session_id OR OLD.scene_id IS NOT NEW.scene_id BEGIN
    SELECT RAISE(ABORT,'Environment scope is immutable; remove and insert facts');
END;
CREATE TRIGGER env_grid_scope_immutable BEFORE UPDATE OF session_id,scene_id ON mechanical_scenes
WHEN OLD.session_id IS NOT NEW.session_id OR OLD.scene_id IS NOT NEW.scene_id BEGIN
    SELECT RAISE(ABORT,'Spatial scene identity is immutable');
END;
CREATE TRIGGER env_object_identity_immutable BEFORE UPDATE OF session_id,object_id ON objects
WHEN OLD.session_id IS NOT NEW.session_id OR OLD.object_id IS NOT NEW.object_id BEGIN
    SELECT RAISE(ABORT,'Object identity is immutable');
END;

CREATE TRIGGER env_facts_insert AFTER INSERT ON scene_environments BEGIN
    INSERT INTO scene_environment_versions VALUES (NEW.session_id,NEW.scene_id,
        env_source_key(NULL) IS NOT env_source_key(NEW.facts_json),
        (env_protection_key(NULL) IS NOT env_protection_key(NEW.facts_json)) OR
        (env_wind_key(NULL) IS NOT env_wind_key(NEW.facts_json)))
    ON CONFLICT(session_id,scene_id) DO UPDATE SET
        source_version=source_version+excluded.source_version,
        protection_version=protection_version+excluded.protection_version;
END;
CREATE TRIGGER env_facts_update AFTER UPDATE OF facts_json ON scene_environments
WHEN OLD.facts_json IS NOT NEW.facts_json BEGIN
    INSERT INTO scene_environment_versions VALUES (NEW.session_id,NEW.scene_id,
        env_source_key(OLD.facts_json) IS NOT env_source_key(NEW.facts_json),
        (env_protection_key(OLD.facts_json) IS NOT env_protection_key(NEW.facts_json)) OR
        (env_wind_key(OLD.facts_json) IS NOT env_wind_key(NEW.facts_json)))
    ON CONFLICT(session_id,scene_id) DO UPDATE SET
        source_version=source_version+excluded.source_version,
        protection_version=protection_version+excluded.protection_version;
END;
CREATE TRIGGER env_facts_delete AFTER DELETE ON scene_environments BEGIN
    INSERT INTO scene_environment_versions VALUES (OLD.session_id,OLD.scene_id,
        env_source_key(OLD.facts_json) IS NOT env_source_key(NULL),
        (env_protection_key(OLD.facts_json) IS NOT env_protection_key(NULL)) OR
        (env_wind_key(OLD.facts_json) IS NOT env_wind_key(NULL)))
    ON CONFLICT(session_id,scene_id) DO UPDATE SET
        source_version=source_version+excluded.source_version,
        protection_version=protection_version+excluded.protection_version;
END;
CREATE TRIGGER env_grid_insert AFTER INSERT ON mechanical_scenes BEGIN
    INSERT INTO scene_environment_versions VALUES (NEW.session_id,NEW.scene_id,0,1)
    ON CONFLICT(session_id,scene_id) DO UPDATE SET protection_version=protection_version+1;
END;
CREATE TRIGGER env_grid_update AFTER UPDATE OF state_json,spatial_enabled ON mechanical_scenes
WHEN json_extract(OLD.state_json,'$.grid') IS NOT json_extract(NEW.state_json,'$.grid')
OR OLD.spatial_enabled IS NOT NEW.spatial_enabled BEGIN
    INSERT INTO scene_environment_versions VALUES (NEW.session_id,NEW.scene_id,0,1)
    ON CONFLICT(session_id,scene_id) DO UPDATE SET protection_version=protection_version+1;
END;
CREATE TRIGGER env_grid_delete AFTER DELETE ON mechanical_scenes BEGIN
    INSERT INTO scene_environment_versions VALUES (OLD.session_id,OLD.scene_id,0,1)
    ON CONFLICT(session_id,scene_id) DO UPDATE SET protection_version=protection_version+1;
END;
-- With no formal provider, source membership can change unknown completeness.
-- Formal providers explicitly enumerate contributors and watch their inputs.
CREATE TRIGGER env_membership_insert AFTER INSERT ON objects
WHEN NEW.scene_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environment_provider_bindings WHERE session_id=NEW.session_id
    AND scene_id=NEW.scene_id AND role='protection' AND descriptor!='none') BEGIN
    INSERT INTO scene_environment_versions VALUES (NEW.session_id,NEW.scene_id,0,1)
    ON CONFLICT(session_id,scene_id) DO UPDATE SET protection_version=protection_version+1;
END;
CREATE TRIGGER env_membership_delete AFTER DELETE ON objects
WHEN OLD.scene_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environment_provider_bindings WHERE session_id=OLD.session_id
    AND scene_id=OLD.scene_id AND role='protection' AND descriptor!='none') BEGIN
    INSERT INTO scene_environment_versions VALUES (OLD.session_id,OLD.scene_id,0,1)
    ON CONFLICT(session_id,scene_id) DO UPDATE SET protection_version=protection_version+1;
END;
CREATE TRIGGER env_membership_update AFTER UPDATE OF scene_id ON objects
WHEN OLD.scene_id IS NOT NEW.scene_id BEGIN
    INSERT INTO scene_environment_versions
    SELECT NEW.session_id,scene_id,0,1 FROM (
        SELECT OLD.scene_id AS scene_id UNION SELECT NEW.scene_id
    ) AS scenes WHERE scene_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM environment_provider_bindings b WHERE b.session_id=NEW.session_id
        AND b.scene_id=scenes.scene_id AND b.role='protection' AND b.descriptor!='none')
    ON CONFLICT(session_id,scene_id) DO UPDATE SET protection_version=protection_version+1;
END;
PRAGMA user_version = 36;
