-- Effects own timed spatial facts. World alone owns the positions of attached objects.
CREATE TABLE spatial_light_effects (
    session_id TEXT NOT NULL,
    effect_id TEXT NOT NULL,
    caster_id TEXT NOT NULL,
    spell_source_id TEXT NOT NULL,
    spell_id TEXT NOT NULL,
    slot_level INTEGER NOT NULL CHECK(slot_level BETWEEN 0 AND 9),
    radius_ft INTEGER NOT NULL CHECK(radius_ft > 0),
    shape TEXT NOT NULL CHECK(shape IN ('sphere','emanation')),
    environment_json TEXT NOT NULL CHECK(json_valid(environment_json)),
    fixed_scene_id TEXT,
    fixed_cell TEXT,
    object_id TEXT,
    expires_tick INTEGER NOT NULL CHECK(expires_tick >= 0),
    concentration_id TEXT,
    PRIMARY KEY(session_id, effect_id),
    UNIQUE(session_id, concentration_id),
    FOREIGN KEY(session_id, caster_id) REFERENCES mechanical_actors(session_id, actor_id),
    FOREIGN KEY(session_id, fixed_scene_id) REFERENCES scene_instances(session_id, scene_id),
    FOREIGN KEY(session_id, object_id) REFERENCES objects(session_id, object_id) ON DELETE CASCADE,
    FOREIGN KEY(session_id, concentration_id) REFERENCES effect_concentrations(session_id, concentration_id),
    CHECK((object_id IS NULL AND fixed_scene_id IS NOT NULL AND fixed_cell IS NOT NULL) OR
          (object_id IS NOT NULL AND fixed_scene_id IS NULL AND fixed_cell IS NULL))
);
-- SessionMemory cold loads use null-safe IS predicates for both anchor fields.
CREATE INDEX spatial_light_effects_on_scene
    ON spatial_light_effects(session_id, fixed_scene_id, effect_id);
CREATE INDEX spatial_light_effects_on_object
    ON spatial_light_effects(session_id, object_id, effect_id);
CREATE INDEX spatial_light_effects_by_spell
    ON spatial_light_effects(session_id, caster_id, spell_id, effect_id);
CREATE INDEX spatial_light_effects_deadline
    ON spatial_light_effects(session_id, expires_tick, effect_id);
-- Point spell contributions remain Effects facts; these indexes only select
-- exact fixed origins for local sight and non-obscuring light independently.
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

-- Derived scene index for effect-bearing objects. World still owns the position.
CREATE TABLE magic_light_anchors (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    scene_id TEXT,
    PRIMARY KEY(session_id, object_id),
    FOREIGN KEY(session_id, object_id) REFERENCES objects(session_id, object_id) ON DELETE CASCADE
);
CREATE INDEX magic_light_anchors_on_scene ON magic_light_anchors(session_id, scene_id, object_id);

CREATE TRIGGER add_magic_light_anchor AFTER INSERT ON spatial_light_effects
WHEN NEW.object_id IS NOT NULL BEGIN
    INSERT INTO magic_light_anchors VALUES (NEW.session_id, NEW.object_id, (
        WITH RECURSIVE ancestors(scene_id,carrier_id,container_id) AS (
            SELECT scene_id,carrier_id,container_id FROM objects
            WHERE session_id=NEW.session_id AND object_id=NEW.object_id
            UNION SELECT o.scene_id,o.carrier_id,o.container_id FROM objects o JOIN ancestors a
                ON o.object_id=a.container_id WHERE o.session_id=NEW.session_id
        )
        SELECT coalesce(p.scene_id,a.current_scene_id) FROM ancestors p LEFT JOIN actors a
            ON a.session_id=NEW.session_id AND a.actor_id=p.carrier_id WHERE p.container_id IS NULL
    ))
    ON CONFLICT(session_id, object_id) DO NOTHING;
END;

CREATE TRIGGER relocate_magic_light_object AFTER UPDATE OF scene_id,carrier_id,container_id ON objects
WHEN (OLD.scene_id IS NOT NEW.scene_id OR OLD.carrier_id IS NOT NEW.carrier_id
      OR OLD.container_id IS NOT NEW.container_id)
  AND EXISTS (SELECT 1 FROM magic_light_anchors WHERE session_id=NEW.session_id) BEGIN
    UPDATE magic_light_anchors SET scene_id=(
        WITH RECURSIVE ancestors(scene_id,carrier_id,container_id) AS (
            SELECT NEW.scene_id,NEW.carrier_id,NEW.container_id
            UNION SELECT o.scene_id,o.carrier_id,o.container_id FROM objects o JOIN ancestors a
                ON o.object_id=a.container_id WHERE o.session_id=NEW.session_id
        )
        SELECT coalesce(p.scene_id,a.current_scene_id) FROM ancestors p LEFT JOIN actors a
            ON a.session_id=NEW.session_id AND a.actor_id=p.carrier_id WHERE p.container_id IS NULL
    ) WHERE session_id=NEW.session_id AND object_id IN (
        WITH RECURSIVE descendants(object_id) AS (
            SELECT NEW.object_id
            UNION SELECT o.object_id FROM objects o JOIN descendants d ON o.container_id=d.object_id
                WHERE o.session_id=NEW.session_id
        ) SELECT object_id FROM descendants
    );
END;

CREATE TRIGGER relocate_magic_light_carried AFTER UPDATE OF current_scene_id ON actors
WHEN OLD.current_scene_id IS NOT NEW.current_scene_id
  AND EXISTS (SELECT 1 FROM magic_light_anchors WHERE session_id=NEW.session_id) BEGIN
    UPDATE magic_light_anchors SET scene_id=NEW.current_scene_id
    WHERE session_id=NEW.session_id AND object_id IN (
        WITH RECURSIVE descendants(object_id) AS (
            SELECT object_id FROM objects WHERE session_id=NEW.session_id AND carrier_id=NEW.actor_id
            UNION SELECT o.object_id FROM objects o JOIN descendants d ON o.container_id=d.object_id
                WHERE o.session_id=NEW.session_id
        ) SELECT object_id FROM descendants
    );
END;

CREATE TRIGGER end_spatial_light_effect AFTER DELETE ON spatial_light_effects BEGIN
    DELETE FROM magic_light_anchors WHERE session_id = OLD.session_id AND object_id = OLD.object_id
      AND NOT EXISTS (SELECT 1 FROM spatial_light_effects
                      WHERE session_id = OLD.session_id AND object_id = OLD.object_id);
    DELETE FROM effect_concentrations WHERE session_id = OLD.session_id
      AND concentration_id = OLD.concentration_id;
END;

PRAGMA user_version = 57;
